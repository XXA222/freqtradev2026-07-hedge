"""Deterministic R3.1 acceptance report builder."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.execution.ownership import ExecutionOrderOwnershipRegistry
from freqtrade.hedge.integration.production_main_loop import (
    ExecutionEngineKind,
    HedgeExecutionMode,
    ProductionEquivalentHedgeMainLoop,
)
from freqtrade.hedge.integration.strategy_state import InMemoryStrategyStateStore
from freqtrade.hedge.planning.context import (
    ActiveOrder,
    IntentAction,
    LegPosition,
    MarketSnapshot,
    OrderSide,
    PlannerConfig,
    PlanningContext,
    PositionBucket,
    PositionSide,
    StrategyLegState,
    WalletSnapshot,
)

ACCOUNT = "binance-usdm:r31-acceptance"
NOW = datetime(2026, 8, 2, 15, 30, tzinfo=UTC)


def load_r30_report(path: str | Path) -> Mapping[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("R3.0 report must be a JSON object")
    if payload.get("status") != "CORE_COMPLETE_LIVE_LOCKED":
        raise ValueError("R3.0 prerequisite status is not CORE_COMPLETE_LIVE_LOCKED")
    if payload.get("production_execution_core_complete") is not True:
        raise ValueError("R3.0 production execution core is incomplete")
    if payload.get("live_exchange_write") != "LOCKED":
        raise ValueError("R3.0 live exchange write is not locked")
    return payload


def build_acceptance_report(r30_report: str | Path) -> dict[str, Any]:
    prerequisite = load_r30_report(r30_report)
    runtime = build_integrated_fake_runtime()
    state_store = InMemoryStrategyStateStore()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_SIMULATED,
        engine_kind=ExecutionEngineKind.SIMULATED,
        state_store=state_store,
    )
    first = loop.run_cycle(_context())
    submit_calls_after_first = len(runtime.exchange.submit_calls)
    replay = loop.run_cycle(_context())
    recovery = loop.recover_pending(symbol="ETHUSDT")
    emergency = loop.emergency_stop(reason="R3.1 acceptance", actor="r31-toolkit")

    external_runtime = build_integrated_fake_runtime()
    external_loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=external_runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(external_runtime.store),
        kill_switch=external_runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_SIMULATED,
        engine_kind=ExecutionEngineKind.SIMULATED,
    )
    external_cycle = external_loop.run_cycle(
        _context(active=(_external_active_order(),))
    )

    locked_runtime = build_integrated_fake_runtime()
    locked_loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=locked_runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(locked_runtime.store),
        kill_switch=locked_runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        engine_kind=ExecutionEngineKind.PRODUCTION,
    )
    locked_cycle = locked_loop.run_cycle(_context())

    checks = {
        "planner_to_execution_both_legs": _pass(
            bool(first.submissions)
            and {item.order.intent.position_side.value for item in first.submissions}
            == {"LONG", "SHORT"}
        ),
        "stable_idempotency_no_resubmit": _pass(
            len(runtime.exchange.submit_calls) == submit_calls_after_first
            and bool(replay.submissions)
            and all(item.idempotent_replay for item in replay.submissions)
        ),
        "strategy_state_committed": _pass(first.strategy_state_committed),
        "recovery_queries_without_resubmit": _pass(
            recovery.inspected == len(first.submissions)
            and len(runtime.exchange.submit_calls) == submit_calls_after_first
            and not recovery.errors
        ),
        "external_order_never_canceled": _pass(
            external_cycle.external_order_ids == ("manual-order-1",)
            and "manual-order-1" not in external_runtime.exchange.cancel_calls
        ),
        "external_side_blocks_new_risk": _pass(
            bool(external_cycle.blocked_submit_intent_ids)
            and all(
                item.order.intent.position_side.value == "SHORT"
                for item in external_cycle.submissions
            )
        ),
        "kill_switch_cancels_managed_only": _pass(
            emergency.kill_switch.mode.value == "HALTED"
            and len(emergency.canceled) == len(first.submissions)
            and not emergency.errors
        ),
        "production_locked_zero_writes": _pass(
            locked_cycle.writes_attempted == 0
            and bool(locked_cycle.blocked_submit_intent_ids)
            and not locked_runtime.exchange.submit_calls
            and not locked_runtime.exchange.cancel_calls
        ),
        "fixed_perpetual_allowlist": _pass(
            tuple(prerequisite.get("allowed_symbols", ())) == ("BTCUSDT", "ETHUSDT")
        ),
    }
    failures = [name for name, value in checks.items() if value != "PASS"]
    report: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": "3.1.1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "MAIN_LOOP_COMPLETE_LIVE_LOCKED" if not failures else "FAIL",
        "production_main_loop_complete": not failures,
        "live_exchange_write": "LOCKED",
        "real_exchange_order_requests_sent": 0,
        "supported_modes": [item.value for item in HedgeExecutionMode],
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "checks": checks,
        "required_failures": failures,
        "evidence": {
            "r30_status": prerequisite.get("status"),
            "r30_report_sha256": prerequisite.get("report_sha256"),
            "first_cycle_submissions": len(first.submissions),
            "idempotent_replays": sum(item.idempotent_replay for item in replay.submissions),
            "recovery_inspected": recovery.inspected,
            "emergency_cancellations": len(emergency.canceled),
            "external_orders_observed": len(external_cycle.external_order_ids),
            "locked_writes_attempted": locked_cycle.writes_attempted,
        },
        "safety": {
            "external_order_ownership": "DURABLE_STORE_AUTHORITATIVE",
            "orphan_prefix_policy": "DO_NOT_CANCEL",
            "external_order_policy": "OBSERVE_AND_BLOCK_NEW_RISK_ON_SAME_SIDE",
            "cycle_ordering": "CANCEL_BEFORE_SUBMIT",
            "strategy_state_commit": "ONLY_AFTER_ACTION_SUCCESS",
            "restart_policy": "QUERY_OR_RESOLVE_WITHOUT_RESUBMIT",
            "real_exchange_access": "NONE",
        },
    }
    return report


def write_report_atomic(payload: Mapping[str, Any], destination: str | Path) -> str:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii") + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256(encoded).hexdigest()


def _pass(condition: object) -> str:
    return "PASS" if bool(condition) else "FAIL"


def _context(*, active: tuple[ActiveOrder, ...] = ()) -> PlanningContext:
    return PlanningContext(
        market=MarketSnapshot(
            symbol="ETH/USDT:USDT",
            timestamp=NOW,
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            mark=Decimal("100"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
        ),
        wallet=WalletSnapshot(
            balance=Decimal("1000"),
            equity=Decimal("1000"),
            available_balance=Decimal("1000"),
            long=LegPosition(PositionSide.LONG),
            short=LegPosition(PositionSide.SHORT),
            active_orders=active,
            leverage=Decimal("3"),
        ),
        config=PlannerConfig(cooldown_seconds=0, max_grid_layers=1),
        long_state=StrategyLegState(PositionSide.LONG),
        short_state=StrategyLegState(PositionSide.SHORT),
        long_signal=Decimal("1"),
        short_signal=Decimal("1"),
    )


def _external_active_order() -> ActiveOrder:
    return ActiveOrder(
        order_id="manual-order-1",
        client_order_id="manual-order-1",
        symbol="ETH/USDT:USDT",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        price=Decimal("100"),
        reduce_only=False,
        bucket=PositionBucket.CORE,
        action=IntentAction.OPEN,
        created_at=NOW,
    )
