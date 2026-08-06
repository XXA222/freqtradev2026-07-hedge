from decimal import Decimal

from freqtrade.enums.hedge import PositionAction, PositionSide
from freqtrade.hedge.domain import HedgeAction, HedgeActionPlan, PositionKey
from freqtrade.hedge.local_reduce_only import calculate_safe_reduce
from freqtrade.hedge.target_position import (
    PendingOrder,
    TargetPosition,
    calculate_target_delta,
    target_idempotency_key,
)


def test_frozen_action_is_not_mutated_by_plan() -> None:
    original = HedgeAction(
        "ETH/USDT:USDT",
        PositionSide.LONG,
        PositionAction.REDUCE,
        "1",
    )
    original_hash = hash(original)
    plan = HedgeActionPlan([original])
    assert hash(original) == original_hash
    assert plan.actions[0].action_group_id == plan.action_group_id
    assert original.action_group_id != plan.action_group_id


def test_position_key_contains_account() -> None:
    left = PositionKey("binance", "ETH/USDT:USDT", PositionSide.LONG, "a")
    right = PositionKey("binance", "ETH/USDT:USDT", PositionSide.LONG, "b")
    assert left != right


def test_target_delta_accounts_for_pending_orders() -> None:
    key = PositionKey("binance", "ETH/USDT:USDT", PositionSide.LONG)
    delta = calculate_target_delta(
        target_quantity=Decimal("12"),
        confirmed_quantity=Decimal("10"),
        pending_orders=[
            PendingOrder("increase", key, PositionAction.INCREASE, Decimal("1")),
            PendingOrder("reduce", key, PositionAction.REDUCE, Decimal("0.5")),
        ],
    )
    assert delta.effective_delta == Decimal("1.5")
    assert delta.action is PositionAction.INCREASE


def test_idempotency_key_is_stable() -> None:
    target = TargetPosition(
        PositionKey("binance", "ETH/USDT:USDT", PositionSide.SHORT),
        Decimal("2"),
        "grid",
        "strategy",
    )
    first = target_idempotency_key(
        cycle_id="cycle-1",
        target=target,
        action=PositionAction.OPEN,
    )
    second = target_idempotency_key(
        cycle_id="cycle-1",
        target=target,
        action=PositionAction.OPEN,
    )
    assert first == second


def test_local_reduce_only_clips() -> None:
    result = calculate_safe_reduce(
        requested_quantity=Decimal("8"),
        confirmed_quantity=Decimal("10"),
        pending_reduce_quantity=Decimal("4"),
    )
    assert result.allowed_quantity == Decimal("6")
    assert result.clipped
