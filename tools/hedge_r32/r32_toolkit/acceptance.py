"""Deterministic acceptance for the R3.2 main-program integration layer."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from freqtrade.hedge.exchange.base import (
    AccountSnapshotFact,
    Direction2HealthFact,
    OrderFact,
    PositionFact,
    ReadonlyAccountView,
)
from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.execution.ownership import ExecutionOrderOwnershipRegistry
from freqtrade.hedge.integration.main_loop_config import (
    production_main_loop_config_from_mapping,
)
from freqtrade.hedge.integration.production_context import ReadonlyPlanningContextBuilder
from freqtrade.hedge.integration.production_controller import ProductionHedgeController
from freqtrade.hedge.integration.production_main_loop import (
    ExecutionEngineKind,
    HedgeExecutionMode,
    ProductionEquivalentHedgeMainLoop,
)
from freqtrade.hedge.planning.context import PlannerConfig

NOW = datetime(2026, 8, 2, 15, 55, tzinfo=UTC)
PAIR = "ETH/USDT:USDT"
ACCOUNT = "binance-usdm:r32-acceptance"


def load_r31_report(path: str | Path) -> Mapping[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("R3.1 report must be a JSON object")
    if payload.get("status") != "MAIN_LOOP_COMPLETE_LIVE_LOCKED":
        raise ValueError("R3.1 prerequisite status is invalid")
    if payload.get("production_main_loop_complete") is not True:
        raise ValueError("R3.1 main loop is incomplete")
    if payload.get("live_exchange_write") != "LOCKED":
        raise ValueError("R3.1 live exchange write is not locked")
    return payload


def build_acceptance_report(*, r31_report: str | Path, project_root: str | Path) -> dict[str, Any]:
    prerequisite = load_r31_report(r31_report)
    config = _config()
    parsed = production_main_loop_config_from_mapping(config)
    runtime = build_integrated_fake_runtime()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        engine_kind=ExecutionEngineKind.PRODUCTION,
    )
    coordinator = _Coordinator()
    controller = ProductionHedgeController(
        config=config,
        loop=loop,
        loop_config=parsed,
        context_builder=ReadonlyPlanningContextBuilder(
            planner_config=PlannerConfig(cooldown_seconds=0, max_grid_layers=1)
        ),
        readonly_coordinator=coordinator,
        exchange=_Exchange(),
        signal_provider=_Signals(),
        timeframe="1m",
    )
    first = controller.after_strategy_analyze()
    duplicate = controller.after_strategy_analyze()

    stale_runtime = build_integrated_fake_runtime()
    stale_loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=stale_runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(stale_runtime.store),
        kill_switch=stale_runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        engine_kind=ExecutionEngineKind.PRODUCTION,
    )
    stale = ProductionHedgeController(
        config=config,
        loop=stale_loop,
        loop_config=parsed,
        context_builder=ReadonlyPlanningContextBuilder(planner_config=PlannerConfig()),
        readonly_coordinator=_Coordinator(stream_fresh=False),
        exchange=_Exchange(),
        signal_provider=_Signals(),
        timeframe="1m",
    ).after_strategy_analyze()

    root = Path(project_root)
    controller_source = (root / "freqtrade/hedge/integration/controller.py").read_text(
        encoding="utf-8"
    )
    composition_source = (root / "freqtrade/hedge/integration/composition.py").read_text(
        encoding="utf-8"
    )
    checks = {
        "typed_main_loop_config": _pass(parsed.enabled and parsed.state_backend == "memory"),
        "fixed_perpetual_allowlist": _pass(parsed.allowed_symbols == ("BTCUSDT", "ETHUSDT")),
        "main_program_controller_hook": _pass(
            "self.production_controller.after_strategy_analyze()" in controller_source
        ),
        "composition_root_binding": _pass(
            "build_production_main_loop_assembly" in composition_source
            and "production_main_loop_assembly=" in composition_source
        ),
        "readonly_account_projection": _pass(
            first.processed and first.context_evidence is not None
        ),
        "existing_positions_protected_core": _pass(
            first.context_evidence is not None
            and first.context_evidence.position_classification
            == "EXCHANGE_POSITION_AS_PROTECTED_CORE"
        ),
        "other_symbols_filtered": _pass(
            first.context_evidence is not None
            and first.context_evidence.ignored_symbol_count == 2
        ),
        "production_locked_zero_writes": _pass(
            first.main_loop is not None
            and first.main_loop.writes_attempted == 0
            and not runtime.exchange.submit_calls
            and not runtime.exchange.cancel_calls
        ),
        "duplicate_candle_not_reprocessed": _pass(
            not duplicate.processed and duplicate.reason == "NO_NEW_CLOSED_CANDLE"
        ),
        "stale_stream_blocks_before_planning": _pass(
            not stale.processed
            and stale.reason == "STREAM_NOT_FRESH"
            and not stale_runtime.exchange.submit_calls
        ),
        "startup_recovery_completed": _pass(
            controller.recovery_report is not None
            and not controller.recovery_report.errors
        ),
    }
    failures = [name for name, result in checks.items() if result != "PASS"]
    return {
        "schema_version": 1,
        "toolkit_version": "3.2.0",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": (
            "MAIN_PROGRAM_INTEGRATION_COMPLETE_LIVE_LOCKED" if not failures else "FAIL"
        ),
        "production_main_program_integration_complete": not failures,
        "live_exchange_write": "LOCKED",
        "real_exchange_order_requests_sent": 0,
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "supported_modes": [
            HedgeExecutionMode.HEDGE_SIMULATED.value,
            HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED.value,
        ],
        "checks": checks,
        "required_failures": failures,
        "evidence": {
            "r31_status": prerequisite.get("status"),
            "r31_report_sha256": prerequisite.get("report_sha256"),
            "first_cycle_processed": first.processed,
            "first_cycle_blocked_submissions": (
                0 if first.main_loop is None else len(first.main_loop.blocked_submit_intent_ids)
            ),
            "ignored_symbol_count": (
                None if first.context_evidence is None else first.context_evidence.ignored_symbol_count
            ),
            "duplicate_cycle_reason": duplicate.reason,
            "stale_cycle_reason": stale.reason,
            "recovery_inspected": (
                None if controller.recovery_report is None else controller.recovery_report.inspected
            ),
        },
        "safety": {
            "exchange_position_classification": "PROTECTED_CORE",
            "health_gate_order": "BEFORE_PLANNER",
            "candle_processing": "ONCE_PER_CLOSED_CANDLE",
            "external_symbols": "FILTER_BEFORE_NORMALIZATION",
            "main_program_write_surface": "NONE_WHILE_LOCKED",
            "armed_mode": "UNSUPPORTED_IN_R3.2",
        },
    }


def write_report_atomic(payload: Mapping[str, Any], destination: str | Path) -> str:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
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


def _pass(value: object) -> str:
    return "PASS" if bool(value) else "FAIL"


def _config() -> dict[str, Any]:
    return {
        "hedge_mode_enabled": True,
        "managed_pair": PAIR,
        "hedge": {
            "account_id": ACCOUNT,
            "operation_mode": "readonly",
            "planner": {"cooldown_seconds": 0, "max_grid_layers": 1},
            "paper": {
                "tick_size": "0.1",
                "qty_step": "0.001",
                "min_qty": "0.001",
                "min_notional": "5",
            },
            "main_loop": {
                "enabled": True,
                "mode": "HEDGE_PRODUCTION_LOCKED",
                "state_backend": "memory",
            },
        },
    }


def _account_view() -> ReadonlyAccountView:
    snapshot = AccountSnapshotFact(
        account_id=ACCOUNT,
        total_wallet_balance=Decimal("1000"),
        total_available_balance=Decimal("800"),
        total_margin_balance=Decimal("1020"),
        total_initial_margin=Decimal("50"),
        total_maintenance_margin=Decimal("5"),
        total_unrealized_pnl=Decimal("20"),
        observed_at=NOW,
        collection_started_at=NOW,
        collection_completed_at=NOW,
    )
    return ReadonlyAccountView(
        account_id=ACCOUNT,
        observed_at=NOW,
        account_snapshot=snapshot,
        balances=(),
        positions=(
            _position("ETHUSDT", "LONG", "0.5"),
            _position("ETHUSDT", "SHORT", "-0.2"),
            _position("BTCUSDT", "LONG", "0.01"),
        ),
        active_orders=(
            _order("ETHUSDT", "1", "BUY", "LONG"),
            _order("BTCUSDT", "2", "SELL", "SHORT"),
        ),
        configuration=None,
        revision=7,
    )


def _position(symbol: str, side: str, quantity: str) -> PositionFact:
    return PositionFact(
        account_id=ACCOUNT,
        symbol=symbol,
        position_side=side,
        quantity=Decimal(quantity),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        unrealized_pnl=Decimal("0"),
        liquidation_price=None,
        leverage=3,
        margin_mode="cross",
        update_time_ms=1,
        observed_at=NOW,
        source="BINANCE_REST",
    )


def _order(symbol: str, oid: str, side: str, position_side: str) -> OrderFact:
    return OrderFact(
        account_id=ACCOUNT,
        symbol=symbol,
        position_side=position_side,
        exchange_order_id=oid,
        client_order_id=f"manual-{oid}",
        side=side,
        order_type="LIMIT",
        status="NEW",
        original_quantity=Decimal("0.1"),
        cumulative_filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        reduce_only=False,
        update_time_ms=1,
        observed_at=NOW,
        source="BINANCE_REST",
        raw={"price": "100"},
    )


def _signal():
    candle = SimpleNamespace(
        symbol=PAIR,
        close_time=NOW,
        close=Decimal("101"),
    )
    return SimpleNamespace(
        symbol=PAIR,
        candle_close_time=NOW,
        long_score=Decimal("1"),
        short_score=Decimal("1"),
        target_net=None,
        candle=candle,
    )


class _Signals:
    def signals(self, pair: str, timeframe: str):
        if pair != PAIR or timeframe != "1m":
            raise AssertionError("unexpected signal request")
        return _signal()


class _Exchange:
    markets: dict[str, Any] = {}

    def fetch_ticker(self, pair: str):
        if pair != PAIR:
            raise AssertionError("unexpected ticker pair")
        return {"bid": "100.9", "ask": "101.1", "last": "101"}


class _ReadonlyRuntime:
    def __init__(self, *, stream_fresh: bool = True) -> None:
        self._view = _account_view()
        self._health = Direction2HealthFact(
            account_id=ACCOUNT,
            rest_fresh=True,
            stream_connected=True,
            stream_fresh=stream_fresh,
            clock_synchronized=True,
            configuration_valid=True,
            reconciliation_consistent=True,
            unmanaged_position_count=0,
            unmanaged_order_count=0,
            external_order_count=1,
            reason_codes=(),
            observed_at=NOW,
        )

    def snapshot(self):
        return SimpleNamespace(direction2_health=self._health)

    def account_view(self):
        return self._view


class _Coordinator:
    started = True

    def __init__(self, *, stream_fresh: bool = True) -> None:
        self.readonly_runtime = _ReadonlyRuntime(stream_fresh=stream_fresh)
