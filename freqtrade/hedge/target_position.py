from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from freqtrade.enums.hedge import PositionAction
from freqtrade.hedge.domain import PositionKey
from freqtrade.hedge.numeric import require_nonnegative


@dataclass(frozen=True, slots=True)
class PendingOrder:
    order_id: str
    key: PositionKey
    action: PositionAction
    remaining_quantity: Decimal

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ValueError("order_id must not be empty.")
        object.__setattr__(
            self,
            "remaining_quantity",
            require_nonnegative(self.remaining_quantity, field="remaining_quantity"),
        )


@dataclass(frozen=True, slots=True)
class TargetPosition:
    key: PositionKey
    target_quantity: Decimal
    reason: str
    strategy_id: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_quantity",
            require_nonnegative(self.target_quantity, field="target_quantity"),
        )
        if not self.reason.strip():
            raise ValueError("reason must not be empty.")
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must not be empty.")


@dataclass(frozen=True, slots=True)
class TargetDelta:
    target_quantity: Decimal
    confirmed_quantity: Decimal
    pending_increase_quantity: Decimal
    pending_reduce_quantity: Decimal
    effective_delta: Decimal

    @property
    def action(self) -> PositionAction | None:
        if self.effective_delta > 0:
            return PositionAction.OPEN if self.confirmed_quantity == 0 else PositionAction.INCREASE
        if self.effective_delta < 0:
            return (
                PositionAction.CLOSE
                if abs(self.effective_delta) >= self.confirmed_quantity
                else PositionAction.REDUCE
            )
        return None


def calculate_target_delta(
    *,
    target_quantity: Decimal,
    confirmed_quantity: Decimal,
    pending_orders: Iterable[PendingOrder] = (),
) -> TargetDelta:
    target = require_nonnegative(target_quantity, field="target_quantity")
    confirmed = require_nonnegative(confirmed_quantity, field="confirmed_quantity")
    pending_increase = Decimal("0")
    pending_reduce = Decimal("0")
    for order in pending_orders:
        if order.action in {PositionAction.OPEN, PositionAction.INCREASE}:
            pending_increase += order.remaining_quantity
        elif order.action in {PositionAction.REDUCE, PositionAction.CLOSE}:
            pending_reduce += order.remaining_quantity
    return TargetDelta(
        target_quantity=target,
        confirmed_quantity=confirmed,
        pending_increase_quantity=pending_increase,
        pending_reduce_quantity=pending_reduce,
        effective_delta=target - confirmed - pending_increase + pending_reduce,
    )


def target_idempotency_key(
    *,
    cycle_id: str,
    target: TargetPosition,
    action: PositionAction,
) -> str:
    if not cycle_id.strip():
        raise ValueError("cycle_id must not be empty.")
    material = "|".join(
        (
            cycle_id,
            target.strategy_id,
            target.key.exchange,
            target.key.account_id,
            target.key.symbol,
            target.key.position_side.value,
            action.value,
            str(target.target_quantity),
        )
    )
    return sha256(material.encode("utf-8")).hexdigest()[:32]
