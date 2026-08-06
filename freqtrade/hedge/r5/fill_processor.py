from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from freqtrade.hedge.planning.context import PositionBucket, PositionSide, ZERO

from .position_ledger import R5CoreTacticalLedger
from .strategy_store import R5StrategyStateStore


class R5InventoryEventType(StrEnum):
    OPEN_FILL = "OPEN_FILL"
    REDUCE_FILL = "REDUCE_FILL"
    FUNDING = "FUNDING"


@dataclass(frozen=True, slots=True)
class R5InventoryEvent:
    event_id: str
    account_id: str
    symbol: str
    position_side: PositionSide
    event_type: R5InventoryEventType
    quantity: Decimal = ZERO
    price: Decimal = ZERO
    fee: Decimal = ZERO
    funding: Decimal = ZERO
    bucket: PositionBucket = PositionBucket.TACTICAL
    tactical_lot_id: str | None = None
    layer: int = 0
    protected_core_quantity: Decimal = ZERO
    occurred_at: datetime = datetime.min.replace(tzinfo=UTC)

    def __post_init__(self) -> None:
        if not str(self.event_id).strip() or len(str(self.event_id)) > 192:
            raise ValueError("R5.2 inventory event id is invalid")
        if not str(self.account_id).startswith("binance-usdm:"):
            raise ValueError("R5.2 inventory account namespace is invalid")
        if self.symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("R5.2 inventory symbol is not allowlisted")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("R5.2 inventory event timestamp must be timezone-aware")
        numeric = (
            self.quantity,
            self.price,
            self.fee,
            self.funding,
            self.protected_core_quantity,
        )
        if any(not item.is_finite() for item in numeric):
            raise ValueError("R5.2 inventory event values must be finite")
        if self.fee < ZERO or self.protected_core_quantity < ZERO or self.layer < 0:
            raise ValueError("R5.2 inventory event values are invalid")
        if self.event_type in {R5InventoryEventType.OPEN_FILL, R5InventoryEventType.REDUCE_FILL}:
            if self.quantity <= ZERO or self.price <= ZERO:
                raise ValueError("R5.2 fill quantity and price must be positive")
        elif self.quantity != ZERO or self.price != ZERO or self.fee != ZERO:
            raise ValueError("R5.2 funding events cannot carry fill fields")

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "event_id": self.event_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "position_side": self.position_side.value,
            "event_type": self.event_type.value,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "fee": str(self.fee),
            "funding": str(self.funding),
            "bucket": self.bucket.value,
            "tactical_lot_id": self.tactical_lot_id,
            "layer": self.layer,
            "protected_core_quantity": str(self.protected_core_quantity),
            "occurred_at": self.occurred_at.isoformat(),
        }


class R5InventoryProcessor:
    """Idempotently projects exchange fills and funding into core/tactical inventory."""

    def __init__(self, store: R5StrategyStateStore) -> None:
        self.store = store

    def apply(self, event: R5InventoryEvent):
        def transform(current):
            if event.event_type is R5InventoryEventType.OPEN_FILL:
                return R5CoreTacticalLedger.open_fill(
                    current,
                    bucket=event.bucket,
                    quantity=event.quantity,
                    price=event.price,
                    fee=event.fee,
                    tactical_lot_id=event.tactical_lot_id,
                    layer=event.layer,
                    opened_at=event.occurred_at,
                )
            if event.event_type is R5InventoryEventType.REDUCE_FILL:
                return R5CoreTacticalLedger.reduce_fill(
                    current,
                    bucket=event.bucket,
                    quantity=event.quantity,
                    price=event.price,
                    fee=event.fee,
                    tactical_lot_id=event.tactical_lot_id,
                    protected_core_quantity=event.protected_core_quantity,
                )
            return R5CoreTacticalLedger.allocate_funding(current, event.funding)

        return self.store.apply_inventory_event(
            event_id=event.event_id,
            account_id=event.account_id,
            symbol=event.symbol,
            side=event.position_side,
            payload=event.canonical_payload(),
            transform=transform,
        )
