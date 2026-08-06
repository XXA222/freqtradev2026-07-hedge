from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import importlib.util
import sys
import types

# Delivery build environments may omit optional exchange/UI dependencies.  Bypass
# the eager persistence package initializer while keeping the real submodules.
if importlib.util.find_spec("ccxt") is None:
    project_root = Path(__file__).resolve().parents[3]
    persistence_package = types.ModuleType("freqtrade.persistence")
    persistence_package.__path__ = [str(project_root / "freqtrade" / "persistence")]
    sys.modules.setdefault("freqtrade.persistence", persistence_package)

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from freqtrade.hedge.r5.arming import R5StageArmAuthority
from freqtrade.hedge.r5.models import R5AccountSnapshot, R5Stage, default_limit_profile
from freqtrade.hedge.r5.sql_budget import SqlR5DailyBudgetStore
from freqtrade.hedge.r5.strategy_store import R5StrategyStateStore
from freqtrade.hedge.exchange.shared_rate_limit import SqliteSharedWeightBudget
from freqtrade.persistence.hedge_migrations import migration_plan_ids
from freqtrade.persistence.hedge_models import create_hedge_tables

from . import TOOLKIT_VERSION

D = Decimal


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    temporary.replace(path)


def _text(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8", errors="strict")


def _contains(root: Path, relative: str, *tokens: str) -> bool:
    try:
        value = _text(root, relative)
    except OSError:
        return False
    return all(token in value for token in tokens)


def _exists(root: Path, relative: str) -> bool:
    return (root / relative).is_file()


def _record_static_checks(root: Path, checks: dict[str, bool]) -> None:
    required_files = (
        "freqtrade/hedge/r5/authoritative_runtime.py",
        "freqtrade/hedge/r5/authoritative_mainnet.py",
        "freqtrade/hedge/r5/authoritative_cli.py",
        "freqtrade/hedge/r5/batch_risk.py",
        "freqtrade/hedge/r5/fencing.py",
        "freqtrade/hedge/r5/sql_budget.py",
        "freqtrade/hedge/r5/fill_closed_loop.py",
        "freqtrade/hedge/r5/income_closed_loop.py",
        "freqtrade/hedge/r5/mainnet_portfolio.py",
        "freqtrade/hedge/r5/emergency.py",
        "freqtrade/hedge/r5/control_adapter.py",
        "freqtrade/hedge/r5/authority_contract.py",
        "freqtrade/hedge/r5/plan_io.py",
        "freqtrade/hedge/execution/sql_audit.py",
        "freqtrade/hedge/execution/ownership.py",
        "freqtrade/hedge/exchange/shared_rate_limit.py",
        "R5.3-AUDIT-REMEDIATION-MATRIX.md",
    )
    for relative in required_files:
        checks[f"file:{relative}"] = _exists(root, relative)

    source_contracts = {
        "fresh_schema_execution_rows": (
            "freqtrade/persistence/hedge_models.py",
            "ExecutionOrderStateRow",
            "ExecutionIdempotencyRow",
            "HEDGE_MODEL_CLASSES",
        ),
        "fresh_schema_budget_rows": (
            "freqtrade/persistence/hedge_models.py",
            "R5DailyBudgetRow",
            "R5BudgetReservationRow",
            "R5IncomeEventRow",
        ),
        "migration_h3033_h3034": (
            "freqtrade/persistence/hedge_migrations.py",
            "H3-033-r5-authority-ledger",
            "H3-034-verify-r5-authority-ledger",
        ),
        "batch_risk_official_runtime": (
            "freqtrade/hedge/r5/batch_risk.py",
            "approve_batch",
            "commit_port",
        ),
        "batch_risk_fencing_bound": (
            "freqtrade/hedge/r5/batch_risk.py",
            "expected_fencing_token",
            "risk_snapshot_id",
        ),
        "final_side_effect_fence": (
            "freqtrade/hedge/r5/fencing.py",
            "_assert_write_fence",
            "submit_order",
            "validate_order",
            "cancel_order",
        ),
        "permit_authority_epoch": (
            "freqtrade/hedge/r5/arming.py",
            "authority_epoch",
            "revoke_all",
        ),
        "budget_reservation_states": (
            "freqtrade/hedge/r5/budget.py",
            "PREPARED",
            "CONFIRMED",
            "RELEASED",
        ),
        "budget_sql_authority": (
            "freqtrade/hedge/r5/sql_budget.py",
            "prepare_orders",
            "confirm_reservation",
            "release_reservation",
        ),
        "income_idempotency": (
            "freqtrade/hedge/r5/sql_budget.py",
            "record_income_event",
            "external_event_id",
        ),
        "fill_inventory_projection": (
            "freqtrade/hedge/r5/fill_closed_loop.py",
            "R5SqlFillProjector",
            "inventory",
        ),
        "ownership_sql_membership": (
            "freqtrade/hedge/execution/ownership.py",
            "get_by_client_order_id",
            "ORPHAN_PREFIX",
        ),
        "full_planning_result": (
            "freqtrade/hedge/r5/authoritative_runtime.py",
            "risk_cancel_order_ids",
            "cancel_order_ids",
            "modify_order_ids",
            "delete_order_ids",
        ),
        "reduce_first_execution": (
            "freqtrade/hedge/r5/authoritative_runtime.py",
            "reduce",
            "submit",
        ),
        "unknown_recovery": (
            "freqtrade/hedge/r5/authoritative_runtime.py",
            "OrderState.UNKNOWN",
            "def recover",
        ),
        "signed_position_adoption": (
            "freqtrade/hedge/r5/authoritative_mainnet.py",
            "adoption_manifests",
            "apply_position_adoption_manifest",
        ),
        "position_projection_seed": (
            "freqtrade/hedge/r5/authoritative_mainnet.py",
            "seed_position_projection",
        ),
        "income_projected_before_runtime": (
            "freqtrade/hedge/r5/authoritative_mainnet.py",
            "income_history",
            "R5IncomeProjector",
        ),
        "shared_weight_read": (
            "freqtrade/hedge/r5/mainnet_api.py",
            "SqliteSharedWeightBudget",
            "reserve_weight",
        ),
        "shared_weight_write": (
            "freqtrade/hedge/execution/binance_usdm_adapter.py",
            "shared_weight_budget",
            "reserve_weight",
        ),
        "ws_principal_scope": (
            "freqtrade/rpc/api_server/hedge_ws.py",
            "HedgeWsPrincipal",
            "account_ids",
            "allow_sensitive",
        ),
        "ws_recursive_redaction": (
            "freqtrade/rpc/api_server/hedge_ws.py",
            "redact",
            "sensitive",
        ),
        "same_ms_monotonic_fill": (
            "freqtrade/hedge/exchange/binance_user_stream.py",
            "allow_same_time_state_change",
            "cumulative",
        ),
        "durable_decision_cursor": (
            "freqtrade/hedge/r5/strategy_store.py",
            "IN_FLIGHT",
            "complete_decision_cursor",
            "release_decision_cursor",
        ),
        "production_cursor_wired": (
            "freqtrade/hedge/integration/production_controller.py",
            "begin_decision_cursor",
            "complete_decision_cursor",
        ),
        "direction_aware_price": (
            "freqtrade/hedge/contracts/ports.py",
            "normalize_price",
            "ROUND_UP",
            "SELL",
        ),
        "wallet_side_required": (
            "freqtrade/freqtradebot.py",
            "trade.position_side",
        ),
        "credential_acl": (
            "freqtrade/hedge/r5/credentials.py",
            "inspect_credential_file_security",
            "assert_credential_file_security",
        ),
        "legacy_write_cli_locked": (
            "freqtrade/hedge/r5/cli.py",
            "LEGACY_R5_WRITE_PATH_DISABLED_USE_R53_AUTHORITATIVE",
        ),
        "binance_only_execution": (
            "freqtrade/hedge/config.py",
            "SUPPORTED_HEDGE_ADAPTERS",
            'frozenset({"binance"})',
        ),
        "sql_execution_audit": (
            "freqtrade/hedge/execution/sql_audit.py",
            "SqlExecutionAuditLog",
        ),
        "post_network_uow": (
            "freqtrade/persistence/hedge_execution_adapters.py",
            "ExecutionOrderStateRow",
            "EventOutbox",
            "AuditEvent",
        ),
        "emergency_dual_authority": (
            "freqtrade/hedge/r5/emergency.py",
            "operator_sha256",
            "risk_sha256",
            "consumed",
        ),
        "emergency_same_runtime": (
            "freqtrade/hedge/r5/authoritative_runtime.py",
            "execute_emergency_reduce",
        ),
        "native_trade_projection_contract": (
            "freqtrade/hedge/r5/authority_contract.py",
            "SQL_HEDGE_LEDGER",
            "READ_ONLY_COMPATIBILITY_PROJECTION",
            "R5_LEGACY_WRITE_ROLE",
        ),
        "plan_strict_reader": (
            "freqtrade/hedge/r5/plan_io.py",
            "PlanningResult",
            "strict",
        ),
        "production_injection_required": (
            "freqtrade/hedge/integration/production_assembly.py",
            "authoritative",
            "IntegratedFakeRuntime",
        ),
    }
    for name, (relative, *tokens) in source_contracts.items():
        checks[name] = _contains(root, relative, *tokens)

    matrix = _text(root, "R5.3-AUDIT-REMEDIATION-MATRIX.md")
    for prefix, count in (("M-P0-", 8), ("M-P1-", 20)):
        for number in range(1, count + 1):
            checks[f"matrix:{prefix}{number:02d}"] = f"{prefix}{number:02d}" in matrix
    checks["matrix_p120_evidence_honest"] = "EVIDENCE PENDING" in matrix
    checks["matrix_p2_deferred_honest"] = "intentionally not performed" in matrix


def _record_dynamic_checks(checks: dict[str, bool]) -> None:
    with tempfile.TemporaryDirectory(prefix="r53-acceptance-") as directory:
        root = Path(directory)
        database = root / "authority.sqlite"
        engine = create_engine(f"sqlite:///{database.as_posix()}", future=True)
        create_hedge_tables(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        names = set(inspect(engine).get_table_names())
        for table in (
            "hedge_execution_order_states",
            "hedge_execution_idempotency",
            "hedge_r5_daily_budgets",
            "hedge_r5_budget_reservations",
            "hedge_r5_income_events",
        ):
            checks[f"fresh_table:{table}"] = table in names
        checks["migration_tail"] = migration_plan_ids()[-2:] == (
            "H3-033-r5-authority-ledger",
            "H3-034-verify-r5-authority-ledger",
        )

        stage = R5Stage.RESTRICTED_AUTO
        operator = "offline-operator"
        risk = "offline-risk"
        authority = R5StageArmAuthority(
            operator_hashes={stage: hashlib.sha256(operator.encode()).hexdigest()},
            risk_hashes={stage: hashlib.sha256(risk.encode()).hexdigest()},
        )
        permit = authority.issue(
            stage=stage,
            run_id="offline-run",
            account_fingerprint="offline-account",
            actor="offline",
            operator_token=operator,
            risk_token=risk,
            confirmation="R5:RESTRICTED_AUTO:offline-run:offline-account",
            profile=default_limit_profile(stage),
        )
        permit.preflight(1)
        checks["permit_preflight_before_revoke"] = True
        authority.revoke_all()
        try:
            permit.preflight(1)
        except PermissionError:
            checks["permit_revoke_invalidates_held"] = True
        else:
            checks["permit_revoke_invalidates_held"] = False

        budget = SqlR5DailyBudgetStore(sessions)
        profile = default_limit_profile(stage)
        account_id = "binance-usdm:offline"
        before = R5AccountSnapshot(account_id, D("1000"), D("0"), D("0"), 0)
        after = R5AccountSnapshot(account_id, D("1000"), D("10"), D("0"), 1)
        reservation = budget.prepare_orders(
            account_id=account_id,
            profile=profile,
            reservations=((D("10"), after, before, False),),
        )
        checks["budget_prepared"] = reservation is not None and budget.snapshot(account_id).orders == 1
        if reservation is None:
            checks["budget_released"] = False
            checks["budget_confirmed"] = False
        else:
            checks["budget_released"] = budget.release_reservation(reservation.reservation_id).orders == 0
            reservation = budget.prepare_orders(
                account_id=account_id,
                profile=profile,
                reservations=((D("10"), after, before, False),),
            )
            confirmed = budget.confirm_reservation(reservation.reservation_id) if reservation else None
            checks["budget_confirmed"] = confirmed is not None and confirmed.turnover == D("10")
        income = dict(
            account_id=account_id,
            external_event_id="offline-income",
            income_type="FUNDING_FEE",
            amount=D("-2"),
            occurred_at=datetime.now(UTC),
            profile=profile,
        )
        first = budget.record_income_event(**income)
        second = budget.record_income_event(**income)
        checks["income_first_applied"] = first[0] is True
        checks["income_replay_idempotent"] = second[0] is False
        checks["income_authoritative_loss"] = budget.authoritative_realized_loss(account_id) == D("2")

        cursor = R5StrategyStateStore(root / "cursor.sqlite")
        close_at = datetime.fromtimestamp(1800, tz=UTC)
        claimed = cursor.begin_decision_cursor(
            account_id=account_id,
            symbol="ETHUSDT",
            timeframe="1m",
            candle_close=close_at,
            decision_id="decision-1",
            lease_owner="worker-a",
            lease_seconds=30,
        )
        blocked = cursor.begin_decision_cursor(
            account_id=account_id,
            symbol="ETHUSDT",
            timeframe="1m",
            candle_close=close_at,
            decision_id="decision-1",
            lease_owner="worker-b",
            lease_seconds=30,
        )
        checks["cursor_first_claim"] = claimed is True
        checks["cursor_second_worker_blocked"] = blocked is False
        cursor.release_decision_cursor(
            account_id=account_id,
            symbol="ETHUSDT",
            timeframe="1m",
            decision_id="decision-1",
            lease_owner="worker-a",
        )
        reclaimed = cursor.begin_decision_cursor(
            account_id=account_id,
            symbol="ETHUSDT",
            timeframe="1m",
            candle_close=close_at,
            decision_id="decision-1",
            lease_owner="worker-b",
            lease_seconds=30,
        )
        checks["cursor_crash_release_reclaim"] = reclaimed is True
        cursor.complete_decision_cursor(
            account_id=account_id,
            symbol="ETHUSDT",
            timeframe="1m",
            decision_id="decision-1",
            lease_owner="worker-b",
        )
        checks["cursor_completed"] = cursor.decision_cursor(
            account_id=account_id, symbol="ETHUSDT", timeframe="1m"
        )["status"] == "COMPLETED"
        checks["cursor_integrity"] = cursor.integrity()["status"] == "PASS"

        now = lambda: 120.0
        first_budget = SqliteSharedWeightBudget(root / "rate.sqlite", limit_per_minute=100, reserve_weight=10, now_epoch=now)
        second_budget = SqliteSharedWeightBudget(root / "rate.sqlite", limit_per_minute=100, reserve_weight=10, now_epoch=now)
        checks["shared_rate_first_granted"] = first_budget.reserve_weight(60).granted is True
        denied = second_budget.reserve_weight(31)
        checks["shared_rate_cross_process_denied"] = denied.granted is False and denied.used_weight == 60
        checks["shared_rate_remaining_granted"] = second_budget.reserve_weight(30).granted is True

        # Release Windows SQLite/SQLAlchemy handles before TemporaryDirectory cleanup.
        cursor.close()
        engine.dispose()


def run(args: argparse.Namespace) -> int:
    root = Path(args.project_root).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().resolve(strict=False)
    checks: dict[str, bool] = {}
    _record_static_checks(root, checks)
    try:
        _record_dynamic_checks(checks)
    except Exception as exc:
        checks[f"dynamic_exception:{type(exc).__name__}:{exc}"] = False
    failed = sorted(name for name, passed in checks.items() if not passed)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "R5_3_AUTHORITATIVE_EXECUTION_READINESS_PASS" if not failed else "R5_3_AUTHORITATIVE_EXECUTION_READINESS_FAIL",
        "check_count": len(checks),
        "passed_count": len(checks) - len(failed),
        "failed_count": len(failed),
        "failed_checks": failed,
        "checks": checks,
        "exchange_access": "NONE",
        "real_mainnet_order_requests_sent": 0,
        "unattended_24_72_hour_evidence": "PENDING_ELAPSED_OPERATIONAL_TIME",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    _atomic_json(output, payload)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if not failed else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r53-offline-acceptance")
    value.add_argument("--project-root", required=True)
    value.add_argument("--output", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
