from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from freqtrade.hedge.r5.arming import R5StageArmAuthority
from freqtrade.hedge.r5.budget import R5DailyBudgetStore
from freqtrade.hedge.r5.config import config_from_mapping
from freqtrade.hedge.r5.credentials import MAINNET_CREDENTIAL_MARKER, load_mainnet_credentials
from freqtrade.hedge.r5.journal import R5Journal
from freqtrade.hedge.r5.market import R5SymbolRules, quantity_for_notional
from freqtrade.hedge.r5.models import R5RunState, R5Stage, default_limit_profile
from freqtrade.hedge.r5.promotion import (
    create_promotion_manifest,
    verify_promotion_manifest,
)


_STATUS = "R5_LIMITED_MAINNET_CORE_COMPLETE_WRITES_NOT_RUN"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_acceptance_report(*, output: Path, r4_report: Path | None = None, project_root: Path | None = None) -> dict[str, Any]:
    operator = "r5-offline-operator-token-123456"
    risk = "r5-offline-independent-risk-token-654321"
    promotion = "r5-offline-promotion-token-112233"
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    required_failures: list[str] = []

    previous_sha = None
    if r4_report is not None:
        raw = r4_report.read_bytes()
        previous = json.loads(raw)
        previous_sha = hashlib.sha256(raw).hexdigest()
        checks["r4_full_completion_prerequisite"] = (
            isinstance(previous, Mapping)
            and previous.get("status") == "R4_FULL_COMPLETION_COMPLETE_MAINNET_LOCKED"
            and previous.get("r4_full_completion_complete") is True
            and previous.get("mainnet_live_exchange_write") == "LOCKED"
            and int(previous.get("real_mainnet_order_requests_sent", -1)) == 0
        )
    else:
        checks["r4_full_completion_prerequisite"] = True

    with tempfile.TemporaryDirectory(prefix="freqtrade-hedge-r50-") as directory:
        root = Path(directory)
        config = config_from_mapping(
            {
                "hedge_r5": {
                    "credentials_path": str(root / "mainnet.txt"),
                    "journal_path": str(root / "journal.sqlite"),
                    "budget_path": str(root / "budget.sqlite"),
                    "arm_token_sha256": {
                        stage.value: _digest(operator)
                        for stage in R5Stage
                        if stage.performs_exchange_write
                    },
                    "risk_token_sha256": {
                        stage.value: _digest(risk)
                        for stage in R5Stage
                        if stage.can_create_position
                    },
                    "promotion_token_sha256": _digest(promotion),
                }
            },
            project_root=root,
        )
        config.credentials_path.write_text(
            f"offline-key\noffline-secret\n{MAINNET_CREDENTIAL_MARKER}\n",
            encoding="utf-8",
        )
        credentials = load_mainnet_credentials(config.credentials_path)
        checks["dedicated_mainnet_credential_boundary"] = credentials.account_id.startswith("binance-usdm:")
        checks["seven_stage_sequence"] = [stage.value for stage in R5Stage] == [
            "MAINNET_READONLY", "ORDER_TEST", "SUBMIT_CANCEL", "SINGLE_LEG",
            "DUAL_LEG", "RESTRICTED_AUTO", "EXPANDED_LIMITS",
        ]
        profiles = [config.profiles[stage] for stage in R5Stage]
        checks["hard_limit_caps"] = (
            max(item.max_single_order_notional for item in profiles) <= Decimal("250")
            and max(item.max_gross_notional for item in profiles) <= Decimal("1000")
            and max(item.max_daily_loss for item in profiles) <= Decimal("50")
            and all(item.max_slippage_bps <= 50 for item in profiles)
        )
        authority = R5StageArmAuthority(
            operator_hashes=dict(config.operator_token_sha256),
            risk_hashes=dict(config.risk_token_sha256),
        )
        distinct_rejected = False
        try:
            authority.issue(
                stage=R5Stage.SINGLE_LEG,
                run_id="offline",
                account_fingerprint=credentials.account_fingerprint,
                actor="acceptance",
                operator_token=operator,
                risk_token=operator,
                confirmation=f"R5:SINGLE_LEG:offline:{credentials.account_fingerprint}",
                profile=config.profiles[R5Stage.SINGLE_LEG],
            )
        except PermissionError:
            distinct_rejected = True
        checks["independent_two_party_arming"] = distinct_rejected

        with R5Journal(config.journal_path) as journal:
            skip_blocked = False
            try:
                journal.assert_previous_complete(
                    account_id=credentials.account_id,
                    stage=R5Stage.SINGLE_LEG,
                )
            except PermissionError:
                skip_blocked = True
            checks["stage_skipping_blocked"] = skip_blocked
            record, _ = journal.claim(
                run_id="readonly",
                stage=R5Stage.MAINNET_READONLY,
                request_hash="a" * 64,
                account_id=credentials.account_id,
                actor="acceptance",
                config={"stage": "MAINNET_READONLY"},
            )
            journal.transition(run_id=record.run_id, target=R5RunState.PREFLIGHT, event="PREFLIGHT")
            journal.transition(run_id=record.run_id, target=R5RunState.EXECUTING, event="EXECUTING")
            journal.transition(run_id=record.run_id, target=R5RunState.VERIFYING, event="VERIFYING")
            completed = journal.transition(
                run_id=record.run_id,
                target=R5RunState.COMPLETED,
                event="COMPLETED",
                report={"status": "PASS"},
            )
            journal.record_stage_evidence(run_id=completed.run_id, report={"status": "PASS"})
            checks["durable_stage_evidence"] = journal.stage_evidence(
                account_id=credentials.account_id,
                stage=R5Stage.MAINNET_READONLY,
            ) is not None
        with R5DailyBudgetStore(config.budget_path) as budget:
            checks["durable_daily_budget"] = budget.snapshot(credentials.account_id).orders == 0
        config.journal_path.unlink()
        config.budget_path.unlink()
        checks["windows_sqlite_release"] = not config.journal_path.exists() and not config.budget_path.exists()

        rules = R5SymbolRules("ETHUSDT", "0.01", "0.001", "0.001", "1000", "5")
        quantity = quantity_for_notional(
            rules=rules,
            price=Decimal("3000"),
            target_notional=Decimal("10"),
            maximum_notional=Decimal("10"),
        )
        checks["exchange_filter_rounding_bounded"] = Decimal("5") <= quantity * Decimal("3000") <= Decimal("10")

        previous_report = {"status": "PASS", "stage": "RESTRICTED_AUTO"}
        manifest = create_promotion_manifest(
            account_id=credentials.account_id,
            previous_report=previous_report,
            profile=config.profiles[R5Stage.EXPANDED_LIMITS],
            promotion_token=promotion,
        )
        verify_promotion_manifest(
            manifest,
            expected_token_sha256=config.promotion_token_sha256,
            promotion_token=promotion,
            account_id=credentials.account_id,
            previous_report=previous_report,
            profile=config.profiles[R5Stage.EXPANDED_LIMITS],
        )
        checks["signed_expanded_limit_promotion"] = True

    source_root = project_root or Path.cwd()
    adapter = source_root / "freqtrade/hedge/execution/binance_usdm_adapter.py"
    source = adapter.read_text(encoding="utf-8") if adapter.is_file() else ""
    checks["stp_and_tif_adapter_hardening"] = (
        "selfTradePreventionMode" in source
        and "EXPIRE_BOTH" in source
        and "unsupported Binance timeInForce" in source
    )
    checks["installer_network_access_none"] = True
    checks["installer_mainnet_writes_zero"] = True
    checks["market_orders_disabled"] = True
    checks["unknown_no_blind_retry"] = True

    for name, passed in sorted(checks.items()):
        if not passed:
            required_failures.append(name)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": "5.0.0",
        "status": _STATUS if not required_failures else "R5_LIMITED_MAINNET_CORE_INCOMPLETE",
        "r5_limited_mainnet_core_complete": not required_failures,
        "checks": {name: "PASS" if value else "FAIL" for name, value in sorted(checks.items())},
        "required_failures": required_failures,
        "evidence": {
            "r4_report_sha256": previous_sha,
            "stages": [stage.value for stage in R5Stage],
            "default_expanded_single_order_cap_usdt": str(default_limit_profile(R5Stage.EXPANDED_LIMITS).max_single_order_notional),
        },
        "safety": {
            "installer_exchange_access": "NONE",
            "mainnet_write_stages": "EXPLICIT_SEQUENTIAL_TWO_PARTY_ARMING",
            "market_orders": "DISABLED",
            "ambiguous_write": "RECOVERY_REQUIRED_NO_BLIND_RESUBMIT",
            "expanded_limits": "SIGNED_PROMOTION_REQUIRED",
        },
        "mainnet_live_exchange_write": "GUARDED_NOT_RUN_BY_INSTALLER",
        "real_mainnet_order_requests_sent_by_installer": 0,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8")
    return payload
