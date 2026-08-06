from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from freqtrade.exceptions import OperationalException
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
from freqtrade.hedge.integration.production_assembly import (
    build_production_main_loop_assembly,
)
from freqtrade.hedge.integration.production_context import (
    ReadonlyPlanningContextBuilder,
)
from freqtrade.hedge.integration.production_controller import ProductionHedgeController
from freqtrade.hedge.integration.production_main_loop import (
    ExecutionEngineKind,
    HedgeExecutionMode,
    ProductionEquivalentHedgeMainLoop,
)
from freqtrade.hedge.planning.context import MarketSnapshot, PlannerConfig

NOW = datetime(2026, 8, 2, 15, 55, tzinfo=UTC)
PAIR = "ETH/USDT:USDT"
ACCOUNT = "binance-usdm:test"


def _config(**main_loop):
    raw = {
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
                **main_loop,
            },
        },
    }
    return raw


def _position(symbol: str, side: str, quantity: str, entry: str = "100") -> PositionFact:
    return PositionFact(
        account_id=ACCOUNT,
        symbol=symbol,
        position_side=side,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry),
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
            _position("ETHUSDT", "SHORT", "-0.2", "105"),
            _position("BTCUSDT", "LONG", "0.01", "60000"),
        ),
        active_orders=(
            _order("ETHUSDT", "1", "BUY", "LONG"),
            _order("BTCUSDT", "2", "SELL", "SHORT"),
        ),
        configuration=None,
        revision=7,
    )


def _signal():
    candle = SimpleNamespace(
        symbol=PAIR,
        timeframe="1m",
        open_time=NOW - timedelta(minutes=1),
        close_time=NOW,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("10"),
    )
    return SimpleNamespace(
        symbol=PAIR,
        timeframe="1m",
        candle_close_time=NOW,
        feature_timestamp=NOW,
        long_score=Decimal("1"),
        short_score=Decimal("1"),
        target_net=None,
        model_version="test",
        reason="TEST",
        candle=candle,
    )


def _health(**overrides):
    values = dict(
        account_id=ACCOUNT,
        rest_fresh=True,
        stream_connected=True,
        stream_fresh=True,
        clock_synchronized=True,
        configuration_valid=True,
        reconciliation_consistent=True,
        unmanaged_position_count=0,
        unmanaged_order_count=0,
        external_order_count=1,
        reason_codes=(),
        observed_at=NOW,
    )
    values.update(overrides)
    return Direction2HealthFact(**values)


class _ReadonlyRuntime:
    def __init__(self, *, health=None):
        self.view = _account_view()
        self.health = health or _health()

    def snapshot(self):
        return SimpleNamespace(direction2_health=self.health)

    def account_view(self):
        return self.view


class _Coordinator:
    def __init__(self, *, health=None):
        self.started = True
        self.readonly_runtime = _ReadonlyRuntime(health=health)


class _Exchange:
    markets = {}

    def fetch_ticker(self, pair):
        assert pair == PAIR
        return {"bid": 100.9, "ask": 101.1, "last": 101}


class _Signals:
    def __init__(self):
        self.value = _signal()

    def signals(self, pair, timeframe):
        assert pair == PAIR
        assert timeframe == "1m"
        return self.value


def test_config_accepts_only_locked_or_simulated_fixed_perpetuals():
    parsed = production_main_loop_config_from_mapping(_config())
    assert parsed.mode is HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED
    assert parsed.allowed_symbols == ("BTCUSDT", "ETHUSDT")
    with pytest.raises(OperationalException, match="authoritative_execution_enabled=true"):
        production_main_loop_config_from_mapping(_config(mode="HEDGE_PRODUCTION_ARMED"))
    with pytest.raises(OperationalException, match="only BTCUSDT and ETHUSDT"):
        production_main_loop_config_from_mapping(_config(allowed_symbols=["SOLUSDT"]))


def test_context_builder_filters_other_symbols_and_protects_exchange_positions_as_core():
    builder = ReadonlyPlanningContextBuilder(planner_config=PlannerConfig(cooldown_seconds=0))
    built = builder.build(
        account_view=_account_view(),
        market=MarketSnapshot(
            symbol=PAIR,
            timestamp=NOW,
            bid=Decimal("100.9"),
            ask=Decimal("101.1"),
            mark=Decimal("101"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
        ),
        signal=_signal(),
    )
    assert built.context.wallet.long.quantity == Decimal("0.5")
    assert built.context.wallet.long.core_quantity == Decimal("0.5")
    assert built.context.wallet.long.tactical_quantity == 0
    assert built.context.wallet.short.quantity == Decimal("0.2")
    assert len(built.context.wallet.active_orders) == 1
    assert built.evidence.ignored_symbol_count == 2
    assert built.evidence.position_classification == "EXCHANGE_POSITION_AS_PROTECTED_CORE"


def test_assembly_builds_locked_zero_write_loop_from_real_config():
    assembly = build_production_main_loop_assembly(
        config=_config(),
        session_factory=lambda: None,
    )
    assert assembly is not None
    assert assembly.loop.mode is HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED
    assert assembly.exchange_write_surface == "NONE"


def test_controller_runs_locked_cycle_once_and_deduplicates_candle():
    runtime = build_integrated_fake_runtime()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        engine_kind=ExecutionEngineKind.PRODUCTION,
    )
    loop_config = production_main_loop_config_from_mapping(_config())
    controller = ProductionHedgeController(
        config=_config(),
        loop=loop,
        loop_config=loop_config,
        context_builder=ReadonlyPlanningContextBuilder(
            planner_config=PlannerConfig(cooldown_seconds=0, max_grid_layers=1)
        ),
        readonly_coordinator=_Coordinator(),
        exchange=_Exchange(),
        signal_provider=_Signals(),
        timeframe="1m",
    )
    first = controller.after_strategy_analyze()
    second = controller.after_strategy_analyze()
    assert first.processed is True
    assert first.main_loop is not None
    assert first.main_loop.writes_attempted == 0
    assert runtime.exchange.submit_calls == []
    assert second.processed is False
    assert second.reason == "NO_NEW_CLOSED_CANDLE"


def test_controller_fails_closed_before_planning_when_stream_is_stale():
    runtime = build_integrated_fake_runtime()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        engine_kind=ExecutionEngineKind.PRODUCTION,
    )
    controller = ProductionHedgeController(
        config=_config(),
        loop=loop,
        loop_config=production_main_loop_config_from_mapping(_config()),
        context_builder=ReadonlyPlanningContextBuilder(planner_config=PlannerConfig()),
        readonly_coordinator=_Coordinator(health=_health(stream_fresh=False)),
        exchange=_Exchange(),
        signal_provider=_Signals(),
        timeframe="1m",
    )
    cycle = controller.after_strategy_analyze()
    assert cycle.processed is False
    assert cycle.reason == "STREAM_NOT_FRESH"
    assert runtime.exchange.submit_calls == []

def test_controller_rejects_account_projection_mismatch():
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
    coordinator.readonly_runtime.view = ReadonlyAccountView(
        account_id="binance-usdm:other",
        observed_at=coordinator.readonly_runtime.view.observed_at,
        account_snapshot=coordinator.readonly_runtime.view.account_snapshot,
        balances=coordinator.readonly_runtime.view.balances,
        positions=coordinator.readonly_runtime.view.positions,
        active_orders=coordinator.readonly_runtime.view.active_orders,
        configuration=coordinator.readonly_runtime.view.configuration,
        revision=coordinator.readonly_runtime.view.revision,
    )
    controller = ProductionHedgeController(
        config=_config(),
        loop=loop,
        loop_config=production_main_loop_config_from_mapping(_config()),
        context_builder=ReadonlyPlanningContextBuilder(planner_config=PlannerConfig()),
        readonly_coordinator=coordinator,
        exchange=_Exchange(),
        signal_provider=_Signals(),
        timeframe="1m",
    )
    with pytest.raises(RuntimeError, match="account_id does not match"):
        controller.after_strategy_analyze()
    assert runtime.exchange.submit_calls == []


def test_controller_refuses_unfinished_candle_before_account_projection():
    runtime = build_integrated_fake_runtime()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        engine_kind=ExecutionEngineKind.PRODUCTION,
    )
    signals = _Signals()
    future = datetime.now(UTC) + timedelta(minutes=1)
    signals.value = SimpleNamespace(
        symbol=PAIR,
        timeframe="1m",
        candle_close_time=future,
        feature_timestamp=future,
        long_score=Decimal("1"),
        short_score=Decimal("1"),
        target_net=None,
        model_version="test",
        reason="TEST",
        candle=SimpleNamespace(symbol=PAIR, close_time=future, close=Decimal("101")),
    )
    controller = ProductionHedgeController(
        config=_config(),
        loop=loop,
        loop_config=production_main_loop_config_from_mapping(_config()),
        context_builder=ReadonlyPlanningContextBuilder(planner_config=PlannerConfig()),
        readonly_coordinator=_Coordinator(),
        exchange=_Exchange(),
        signal_provider=signals,
        timeframe="1m",
    )
    cycle = controller.after_strategy_analyze()
    assert cycle.processed is False
    assert cycle.reason == "CANDLE_NOT_CLOSED"
    assert runtime.exchange.submit_calls == []
