from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.execution.ownership import (
    ExecutionOrderOwnershipRegistry,
    OrderOwnership,
)
from freqtrade.hedge.execution.service import PositionSide as ExecutionPositionSide
from freqtrade.hedge.integration.production_main_loop import (
    ExecutionEngineKind,
    HedgeExecutionMode,
    ProductionEquivalentHedgeMainLoop,
)
from freqtrade.hedge.integration.strategy_state import (
    InMemoryStrategyStateStore,
    JsonStrategyStateStore,
)
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

NOW = datetime(2026, 8, 2, 15, 30, tzinfo=UTC)
ACCOUNT = "binance-usdm:test-account"


def context(*, active=(), timestamp=NOW, symbol="ETH/USDT:USDT") -> PlanningContext:
    return PlanningContext(
        market=MarketSnapshot(
            symbol=symbol,
            timestamp=timestamp,
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
            active_orders=tuple(active),
            leverage=Decimal("3"),
        ),
        config=PlannerConfig(cooldown_seconds=0, max_grid_layers=1),
        long_state=StrategyLegState(PositionSide.LONG),
        short_state=StrategyLegState(PositionSide.SHORT),
        long_signal=Decimal("1"),
        short_signal=Decimal("1"),
    )


def active_order(
    client_id: str,
    *,
    side: PositionSide = PositionSide.LONG,
    reduce_only: bool = False,
    order_id: str | None = None,
) -> ActiveOrder:
    action = IntentAction.REDUCE if reduce_only else IntentAction.OPEN
    order_side = (
        OrderSide.SELL
        if side is PositionSide.LONG and reduce_only
        else OrderSide.BUY
        if side is PositionSide.LONG
        else OrderSide.BUY
        if reduce_only
        else OrderSide.SELL
    )
    return ActiveOrder(
        order_id=order_id or client_id,
        client_order_id=client_id,
        symbol="ETH/USDT:USDT",
        position_side=side,
        order_side=order_side,
        quantity=Decimal("0.1"),
        price=Decimal("100"),
        reduce_only=reduce_only,
        bucket=PositionBucket.CORE,
        action=action,
        created_at=NOW,
    )


def build_loop(*, mode=HedgeExecutionMode.HEDGE_SIMULATED, kind=ExecutionEngineKind.SIMULATED):
    runtime = build_integrated_fake_runtime()
    registry = ExecutionOrderOwnershipRegistry(runtime.store)
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=registry,
        kill_switch=runtime.kill_switch,
        mode=mode,
        engine_kind=kind,
        state_store=InMemoryStrategyStateStore(),
    )
    return runtime, registry, loop


def test_simulated_cycle_executes_both_legs_and_commits_state():
    runtime, _, loop = build_loop()
    result = loop.run_cycle(context())
    assert result.successful
    assert result.submissions
    assert {item.order.intent.position_side for item in result.submissions} == {
        ExecutionPositionSide.LONG,
        ExecutionPositionSide.SHORT,
    }
    assert result.strategy_state_committed is True
    assert len(runtime.exchange.submit_calls) == len(result.submissions)


def test_repeated_same_cycle_is_idempotent_without_exchange_resubmit():
    runtime, _, loop = build_loop()
    first = loop.run_cycle(context())
    calls = len(runtime.exchange.submit_calls)
    second = loop.run_cycle(context())
    assert len(runtime.exchange.submit_calls) == calls
    assert second.submissions
    assert all(item.idempotent_replay for item in second.submissions)
    assert [item.order.client_order_id for item in second.submissions] == [
        item.order.client_order_id for item in first.submissions
    ]


def test_external_order_is_never_canceled_and_blocks_new_risk_on_same_side():
    runtime, _, loop = build_loop()
    external = active_order("manual-order-1", side=PositionSide.LONG)
    result = loop.run_cycle(context(active=(external,)))
    assert result.external_order_ids == (external.order_id,)
    assert result.blocked_submit_intent_ids
    assert all(
        item.order.intent.position_side.value == "SHORT" for item in result.submissions
    )
    assert "manual-order-1" not in runtime.exchange.cancel_calls


def test_orphan_managed_prefix_is_not_owned_or_canceled():
    runtime, registry, loop = build_loop()
    orphan = active_order("FTH-ETHUSDT-L0-ORPHAN123456789")
    assert registry.classify(orphan.client_order_id).ownership is OrderOwnership.ORPHAN_PREFIX
    result = loop.run_cycle(context(active=(orphan,)))
    assert result.orphan_order_ids == (orphan.order_id,)
    assert orphan.client_order_id not in runtime.exchange.cancel_calls


def test_production_locked_mode_plans_but_attempts_zero_writes():
    runtime, _, loop = build_loop(
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        kind=ExecutionEngineKind.PRODUCTION,
    )
    result = loop.run_cycle(context())
    assert result.planning is not None
    assert result.blocked_submit_intent_ids
    assert result.writes_attempted == 0
    assert runtime.exchange.submit_calls == []
    assert runtime.exchange.cancel_calls == []


def test_mode_kind_mismatch_fails_fast():
    runtime = build_integrated_fake_runtime()
    with pytest.raises(ValueError, match="simulated engine"):
        ProductionEquivalentHedgeMainLoop(
            account_id=ACCOUNT,
            engine=runtime.engine,
            ownership=ExecutionOrderOwnershipRegistry(runtime.store),
            kill_switch=runtime.kill_switch,
            mode=HedgeExecutionMode.HEDGE_SIMULATED,
            engine_kind=ExecutionEngineKind.PRODUCTION,
        )


def test_recovery_queries_existing_orders_without_resubmission():
    runtime, _, loop = build_loop()
    cycle = loop.run_cycle(context())
    calls = len(runtime.exchange.submit_calls)
    report = loop.recover_pending(symbol="ETHUSDT")
    assert report.inspected == len(cycle.submissions)
    assert report.refreshed
    assert len(runtime.exchange.submit_calls) == calls


def test_emergency_stop_cancels_only_authoritative_managed_orders():
    runtime, _, loop = build_loop()
    cycle = loop.run_cycle(context())
    external = "manual-order-9"
    report = loop.emergency_stop(reason="operator test", actor="tester")
    assert report.kill_switch.mode.value == "HALTED"
    assert len(report.canceled) == len(cycle.submissions)
    assert external not in runtime.exchange.cancel_calls
    assert all(item.order.lifecycle.status.value == "CANCELED" for item in report.canceled)
    resumed = loop.resume_after_emergency(actor="tester", confirmed=True)
    assert resumed.mode.value == "RUNNING"


def test_json_strategy_state_round_trip_is_atomic_ascii(tmp_path):
    store = JsonStrategyStateStore(tmp_path / "state.json")
    value = StrategyLegState(
        side=PositionSide.LONG,
        grid_layers_filled=2,
        last_entry_at=NOW,
        sequence=5,
        unstuck_daily_loss=Decimal("1.25"),
    )
    store.save(value, decision_id="cycle-1")
    assert store.load(PositionSide.LONG) == value
    (tmp_path / "state.json").read_bytes().decode("ascii")
    assert not list(tmp_path.glob("*.tmp"))


def test_state_is_not_committed_when_external_conflict_blocks_actions():
    runtime = build_integrated_fake_runtime()
    state = InMemoryStrategyStateStore()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_SIMULATED,
        engine_kind=ExecutionEngineKind.SIMULATED,
        state_store=state,
    )
    result = loop.run_cycle(context(active=(active_order("manual-long"),)))
    assert result.blocked_submit_intent_ids
    assert result.strategy_state_committed is False
    assert state.load(PositionSide.LONG) is None


def test_non_allowlisted_symbol_is_rejected_before_planning():
    _, _, loop = build_loop()
    with pytest.raises(ValueError, match="allowlist"):
        loop.run_cycle(context(symbol="SOL/USDT:USDT"))


def test_cycle_action_limits_defer_excess_without_silent_loss():
    runtime = build_integrated_fake_runtime()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_SIMULATED,
        engine_kind=ExecutionEngineKind.SIMULATED,
        max_submissions_per_cycle=1,
    )
    result = loop.run_cycle(context(timestamp=NOW + timedelta(minutes=1)))
    assert len(result.submissions) == 1
    assert result.deferred_submit_intent_ids
    assert result.successful is False
    assert result.strategy_state_committed is False


def test_cancel_failure_blocks_following_risk_increase(monkeypatch):
    runtime, registry, loop = build_loop()
    first = loop.run_cycle(context())
    original = first.submissions[0]
    planner_intent = first.planning.submit_orders[0]
    managed_active = ActiveOrder(
        order_id=original.order.client_order_id,
        client_order_id=original.order.client_order_id,
        symbol="ETH/USDT:USDT",
        position_side=planner_intent.position_side,
        order_side=planner_intent.order_side,
        quantity=planner_intent.quantity,
        price=planner_intent.price,
        reduce_only=planner_intent.reduce_only,
        bucket=planner_intent.bucket,
        action=planner_intent.action,
        created_at=NOW,
        layer=planner_intent.layer,
    )
    desired = first.planning.submit_orders[-1]

    class StaticPlanner:
        def plan(self, value):
            base = first.planning
            return replace(
                base,
                submit_orders=(desired,),
                cancel_order_ids=(managed_active.order_id,),
            )

    guarded_loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=registry,
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_SIMULATED,
        engine_kind=ExecutionEngineKind.SIMULATED,
        planner=StaticPlanner(),
        state_store=InMemoryStrategyStateStore(),
    )

    def fail_cancel(*, client_order_id):
        raise TimeoutError(client_order_id)

    monkeypatch.setattr(runtime.exchange, "cancel_order", fail_cancel)
    submit_calls = len(runtime.exchange.submit_calls)
    result = guarded_loop.run_cycle(context(active=(managed_active,)))
    assert result.errors
    assert result.blocked_submit_intent_ids == (desired.intent_id,)
    assert len(runtime.exchange.submit_calls) == submit_calls
    assert result.strategy_state_committed is False
