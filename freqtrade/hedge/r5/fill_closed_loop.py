"""Durable SQL fill -> core/tactical inventory projection for R5.3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal

from sqlalchemy import select

from freqtrade.hedge.planning.context import PositionBucket, PositionSide
from freqtrade.persistence.hedge_models import FillEvent, ExecutionOrderStateRow

from .budget import R5BudgetPort
from .fill_processor import (
    R5InventoryEvent,
    R5InventoryEventType,
    R5InventoryProcessor,
)
from .models import R5LimitProfile


@dataclass(frozen=True, slots=True)
class R5FillProjectionReport:
    inspected: int
    applied: int
    replayed: int
    realized_pnl_delta: Decimal
    fees: Decimal


class R5SqlFillProjector:
    """Project every durable execution fill exactly once into R5 inventory.

    The immutable ``hedge_fill_events`` row is the inventory authority. Bucket and
    tactical lot ownership come from durable execution metadata. Financial daily-loss
    authority is Binance income, so fill projection never increments the SQL loss budget;
    this prevents one economic loss from being counted once by fills and again by Income.
    """

    def __init__(
        self,
        *,
        session_factory: object,
        inventory: R5InventoryProcessor,
        budget: R5BudgetPort | None = None,
        profile: R5LimitProfile | None = None,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if (budget is None) != (profile is None):
            raise ValueError("budget and profile must be supplied together")
        self._session_factory = session_factory
        self._inventory = inventory
        self._budget = budget
        self._profile = profile

    def project(self, *, account_id: str, symbols: tuple[str, ...] = ()) -> R5FillProjectionReport:
        with self._session_factory() as session:  # type: ignore[operator]
            statement = (
                select(FillEvent, ExecutionOrderStateRow)
                .join(
                    ExecutionOrderStateRow,
                    ExecutionOrderStateRow.client_order_id == FillEvent.client_order_id,
                )
                .where(FillEvent.account_id == account_id)
                .order_by(FillEvent.event_time, FillEvent.id)
            )
            if symbols:
                statement = statement.where(FillEvent.symbol.in_(tuple(symbols)))
            rows = tuple(session.execute(statement).all())

        applied = 0
        replayed = 0
        realized_delta = Decimal("0")
        total_fees = Decimal("0")
        for fill, order in rows:
            metadata = json.loads(str(order.metadata_json or "{}"))
            bucket = PositionBucket(str(metadata.get("bucket", "CORE")).upper())
            action = str(fill.action or order.action).upper()
            event_type = (
                R5InventoryEventType.OPEN_FILL
                if action in {"OPEN", "INCREASE"}
                else R5InventoryEventType.REDUCE_FILL
            )
            side = PositionSide(str(fill.position_side).upper())
            before = self._inventory.store.inventory_leg(
                account_id=account_id,
                symbol=str(fill.symbol),
                side=side,
            )
            before_realized = Decimal("0") if before is None else before.realized_pnl
            event = R5InventoryEvent(
                event_id=f"sql-fill:{fill.event_id}",
                account_id=account_id,
                symbol=str(fill.symbol),
                position_side=side,
                event_type=event_type,
                quantity=Decimal(str(fill.quantity)),
                price=Decimal(str(fill.price)),
                fee=Decimal(str(fill.fee_amount)),
                bucket=bucket,
                tactical_lot_id=(
                    None
                    if metadata.get("tactical_lot_id") in (None, "")
                    else str(metadata["tactical_lot_id"])
                ),
                layer=int(metadata.get("layer", 0)),
                protected_core_quantity=Decimal(
                    str(metadata.get("protected_core_quantity", "0"))
                ),
                occurred_at=fill.event_time.replace(tzinfo=UTC),
            )
            result, was_replay = self._inventory.apply(event)
            if was_replay:
                replayed += 1
                continue
            applied += 1
            delta = result.realized_pnl - before_realized
            realized_delta += delta
            total_fees += event.fee
        return R5FillProjectionReport(
            inspected=len(rows),
            applied=applied,
            replayed=replayed,
            realized_pnl_delta=realized_delta,
            fees=total_fees,
        )
