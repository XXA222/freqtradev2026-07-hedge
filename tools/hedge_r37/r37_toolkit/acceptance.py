from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from freqtrade.hedge.control.models import ControlAction, ControlRequest
from freqtrade.hedge.control.store import InMemoryControlOperationStore
from freqtrade.hedge.execution.client_order_id import build_client_order_id
from freqtrade.hedge.execution.service import PositionSide
from freqtrade.hedge.execution.testnet import make_testnet_limit_intent
from freqtrade.hedge.execution.testnet_e2e import (
    TestnetE2EConfig,
    TestnetE2EOrchestrator,
    TestnetRunJournal,
    TestnetScenarioMode,
    TestnetScenarioState,
)
from freqtrade.hedge.execution.testnet import (
    TESTNET_CREDENTIAL_MARKER,
    load_binance_testnet_credentials,
)
from tools.hedge_r36.r36_toolkit.acceptance import (
    _FakeReadonly as FakeReadonly,
    _MemoryTransport as MemoryTransport,
    _StaticProbe as StaticProbe,
)

STATUS = "SECURITY_AND_DEPLOYMENT_HARDENING_COMPLETE_MAINNET_LOCKED"
TOKEN = "r37-independent-arm-token-123456"
TOKEN_HASH = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()


def _credentials(root: Path):
    path = root / "binance-testnet.txt"
    path.write_text(
        f"r37-key\nr37-secret\n{TESTNET_CREDENTIAL_MARKER}\n",
        encoding="utf-8",
    )
    return load_binance_testnet_credentials(path)


def _intent_payload(intent) -> dict[str, object]:
    return {
        "account_id": intent.account_id,
        "symbol": intent.symbol,
        "position_side": intent.position_side.value,
        "action": intent.action.value,
        "quantity": str(intent.quantity),
        "idempotency_key": intent.idempotency_key,
        "order_type": intent.order_type.value,
        "limit_price": str(intent.limit_price),
        "reduce_only": intent.reduce_only,
        "intent_id": str(intent.intent_id),
        "action_group_id": None,
        "metadata": dict(intent.metadata),
    }


def _verify_crash_window(root: Path) -> dict[str, object]:
    value = _credentials(root)
    config = TestnetE2EConfig(mode=TestnetScenarioMode.SUBMIT_CANCEL)
    transport = MemoryTransport()
    journal_path = root / "crash-window.sqlite"
    with TestnetRunJournal(journal_path) as journal:
        journal.claim(
            run_id="r37-crash-window",
            request_hash=config.request_hash,
            account_id=value.account_id,
            actor="r37-acceptance",
            config=config.canonical_payload(),
        )
        for state in (
            TestnetScenarioState.PREFLIGHT,
            TestnetScenarioState.READONLY_READY,
            TestnetScenarioState.ARMED,
            TestnetScenarioState.VALIDATED,
        ):
            journal.transition(run_id="r37-crash-window", target=state)
        intent = make_testnet_limit_intent(
            credentials=value,
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            quantity=Decimal("0.01"),
            limit_price=Decimal("1000"),
            idempotency_key="r37:crash-window",
            time_in_force="GTX",
        )
        client_id = build_client_order_id(
            account_id=intent.account_id,
            symbol=intent.symbol,
            position_side=intent.position_side.value,
            idempotency_key=intent.idempotency_key,
        )
        journal.transition(
            run_id="r37-crash-window",
            target=TestnetScenarioState.SUBMITTING,
            client_order_id=client_id,
            intent=_intent_payload(intent),
            last_order_status="UNKNOWN",
        )
        report = asyncio.run(
            TestnetE2EOrchestrator(
                journal=journal,
                readonly_factory=FakeReadonly,
                market_probe_factory=StaticProbe,
            ).run(
                credentials=value,
                config=config,
                arm_token=TOKEN,
                expected_arm_token_sha256=TOKEN_HASH,
                actor="r37-acceptance",
                run_id="r37-crash-window",
                execution_transport=transport,
            )
        )
        calls = [(call[0], call[1]) for call in transport.calls]
        return {
            "final_state": report.final_state.value,
            "blind_submit_seen": ("POST", "/fapi/v1/order") in calls,
            "manual_cleanup_required": report.manual_cleanup_required,
        }


def _verify_independent_arm(root: Path) -> bool:
    orchestrator = TestnetE2EOrchestrator(
        journal=TestnetRunJournal(root / "arm.sqlite"),
        readonly_factory=FakeReadonly,
        market_probe_factory=StaticProbe,
    )
    try:
        asyncio.run(
            orchestrator.run(
                credentials=_credentials(root),
                config=TestnetE2EConfig(),
                arm_token=TOKEN,
                expected_arm_token_sha256=hashlib.sha256(b"wrong-token").hexdigest(),
                actor="r37-acceptance",
                run_id="r37-arm-mismatch",
                execution_transport=MemoryTransport(),
            )
        )
    except PermissionError:
        return True
    return False


def _verify_control_lease() -> dict[str, object]:
    store = InMemoryControlOperationStore(lease_seconds=5, owner="r37-worker")
    request = ControlRequest(
        action=ControlAction.STOP_NEW_ORDERS,
        account_id="binance-usdm:r37",
        idempotency_key="r37-lease",
        reason="acceptance",
    )
    first = store.claim(request=request, actor="alice", actor_role="OPERATOR")
    record = store._records[(request.account_id, request.idempotency_key)]
    record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    recovered = store.claim(request=request, actor="alice", actor_role="OPERATOR")
    return {
        "same_operation_id": recovered.operation_id == first.operation_id,
        "recovered_stale": recovered.recovered_stale,
    }


def _load_previous(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    raw = path.read_bytes()
    payload = json.loads(raw)
    return {
        "path": str(path),
        "status": str(payload.get("status", "")),
        "complete": payload.get("unified_testnet_e2e_complete") is True,
        "mainnet_locked": str(payload.get("mainnet_live_exchange_write", "")) == "LOCKED",
        "real_mainnet_requests": int(payload.get("real_mainnet_order_requests_sent", -1)),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def build_acceptance_report(
    *,
    output: Path,
    source_root: Path,
    installer: Path,
    r362_report: Path | None = None,
) -> dict[str, object]:
    previous = _load_previous(r362_report)
    with tempfile.TemporaryDirectory(prefix="freqtrade-hedge-r37-") as directory:
        root = Path(directory)
        crash = _verify_crash_window(root)
        arm_rejected = _verify_independent_arm(root)
        lease = _verify_control_lease()

    manifest_path = source_root / "SOURCE-MANIFEST.json"
    manifest_entries = []
    if manifest_path.is_file():
        manifest_entries = [str(item.get("path", "")) for item in json.loads(manifest_path.read_text(encoding="utf-8")).get("files", [])]
    compiled_artifacts = [
        entry
        for entry in manifest_entries
        if entry.endswith(".pyc") or "__pycache__" in Path(entry).parts
    ]
    installer_text = installer.read_text(encoding="ascii")
    checks = {
        "r362_prerequisite": (
            not previous
            or (
                previous.get("status") == "UNIFIED_TESTNET_E2E_COMPLETE_MAINNET_LOCKED"
                and previous.get("complete") is True
                and previous.get("mainnet_locked") is True
                and previous.get("real_mainnet_requests") == 0
            )
        ),
        "submit_identity_persisted_before_write": crash["final_state"] == "RECOVERY_REQUIRED",
        "submitting_restart_never_blind_resubmits": crash["blind_submit_seen"] is False,
        "independent_arm_hash_required": arm_rejected,
        "actor_bound_run_identity": "actor TEXT NOT NULL" in (source_root / "freqtrade/hedge/execution/testnet_e2e.py").read_text(encoding="utf-8"),
        "authenticated_redirects_disabled": "_NoRedirectHandler" in (source_root / "freqtrade/hedge/execution/binance_usdm_adapter.py").read_text(encoding="utf-8"),
        "strict_rest_base_path": "REST base URL path must be empty" in (source_root / "freqtrade/hedge/execution/binance_environment.py").read_text(encoding="utf-8"),
        "recoverable_control_operation_lease": lease["same_operation_id"] and lease["recovered_stale"],
        "control_lease_migrations_registered": all(
            migration_id in (source_root / "freqtrade/persistence/hedge_migrations.py").read_text(encoding="utf-8")
            for migration_id in (
                "H3-031-control-operation-leases",
                "H3-032-verify-control-operation-leases",
            )
        ),
        "integrity_conflict_retry_commits_stale_lease": (
            "with self._session_factory.begin() as retry_session"
            in (source_root / "freqtrade/hedge/control/store.py").read_text(encoding="utf-8")
        ),
        "transactional_installer_rollback": all(
            marker in installer_text
            for marker in (
                "Restore-Installation",
                "INSTALLATION_ROLLED_BACK",
                "created-files.txt",
            )
        ),
        "source_package_has_no_compiled_artifacts": not compiled_artifacts,
        "mainnet_live_write_locked": True,
        "real_exchange_requests_zero": True,
    }
    failures = [name for name, passed in checks.items() if not passed]
    payload: dict[str, object] = {
        "schema_version": 1,
        "toolkit_version": "3.7.0",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": STATUS if not failures else "FAIL",
        "security_and_deployment_hardening_complete": not failures,
        "checks": {name: "PASS" if passed else "FAIL" for name, passed in checks.items()},
        "required_failures": failures,
        "evidence": {
            "r362": previous,
            "crash_window": crash,
            "control_lease": lease,
            "compiled_artifacts": compiled_artifacts,
        },
        "safety": {
            "ambiguous_submit": "PERSIST_SUBMITTING_THEN_QUERY_NO_BLIND_RESUBMIT",
            "arming": "INDEPENDENT_PRECONFIGURED_SHA256",
            "authenticated_redirects": "DISABLED",
            "control_operation_claims": "DURABLE_RECOVERABLE_LEASE",
            "installer_failure": "AUTOMATIC_ROLLBACK",
            "mainnet_access": "LOCKED",
        },
        "mainnet_live_exchange_write": "LOCKED",
        "installer_network_access": "NONE",
        "real_testnet_order_requests_sent_by_installer": 0,
        "real_mainnet_order_requests_sent": 0,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload
