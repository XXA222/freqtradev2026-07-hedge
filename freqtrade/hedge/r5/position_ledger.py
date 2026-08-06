from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from freqtrade.hedge.planning.context import (
    LegPosition,
    PositionBucket,
    PositionSide,
    TacticalLot,
    ZERO,
)


def _weighted_average(q1: Decimal, p1: Decimal, q2: Decimal, p2: Decimal) -> Decimal:
    total = q1 + q2
    if total <= ZERO:
        return ZERO
    return ((q1 * p1) + (q2 * p2)) / total


def _pnl(side: PositionSide, entry: Decimal, exit_price: Decimal, quantity: Decimal) -> Decimal:
    return (exit_price - entry) * quantity * side.direction


class R5CoreTacticalLedger:
    """Pure accounting for core and tactical inventory.

    Core inventory is protected from tactical reductions.  Tactical reductions use the
    explicitly referenced lot when supplied and FIFO otherwise.  The class never infers
    exchange fills and therefore remains deterministic across backtest, dry-run, testnet,
    and production surfaces.
    """

    @staticmethod
    def adopt_as_core(
        *,
        side: PositionSide,
        quantity: Decimal,
        average_price: Decimal,
    ) -> LegPosition:
        if quantity < ZERO or average_price < ZERO or (quantity > ZERO and average_price <= ZERO):
            raise ValueError("R5.2 adopted position is invalid")
        return LegPosition(
            side=side,
            quantity=quantity,
            average_price=average_price if quantity > ZERO else ZERO,
            core_quantity=quantity,
            core_average_price=average_price if quantity > ZERO else ZERO,
            tactical_quantity=ZERO,
            tactical_average_price=ZERO,
        )

    @staticmethod
    def open_fill(
        leg: LegPosition,
        *,
        bucket: PositionBucket,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = ZERO,
        tactical_lot_id: str | None = None,
        layer: int = 0,
        opened_at: datetime | None = None,
    ) -> LegPosition:
        if quantity <= ZERO or price <= ZERO or fee < ZERO:
            raise ValueError("R5.2 opening fill is invalid")
        total_qty = leg.quantity + quantity
        total_average = _weighted_average(leg.quantity, leg.average_price, quantity, price)
        realized = leg.realized_pnl - fee
        if bucket is PositionBucket.CORE:
            core_qty = leg.core_quantity + quantity
            core_avg = _weighted_average(leg.core_quantity, leg.core_average_price, quantity, price)
            tactical_qty = leg.tactical_quantity
            tactical_avg = leg.tactical_average_price
            lots = leg.tactical_lots
        else:
            lot_id = str(tactical_lot_id or "").strip()
            if not lot_id:
                raise ValueError("R5.2 tactical fill requires tactical_lot_id")
            if any(item.lot_id == lot_id for item in leg.tactical_lots):
                raise ValueError("R5.2 tactical lot id already exists")
            timestamp = opened_at or datetime.now(UTC)
            lot = TacticalLot(
                lot_id=lot_id,
                quantity=quantity,
                average_price=price,
                opened_at=timestamp,
                layer=layer,
                fees=fee,
            )
            core_qty = leg.core_quantity
            core_avg = leg.core_average_price
            tactical_qty = leg.tactical_quantity + quantity
            tactical_avg = _weighted_average(
                leg.tactical_quantity,
                leg.tactical_average_price,
                quantity,
                price,
            )
            lots = tuple((*leg.tactical_lots, lot))
        return LegPosition(
            side=leg.side,
            quantity=total_qty,
            average_price=total_average,
            core_quantity=core_qty,
            core_average_price=core_avg,
            tactical_quantity=tactical_qty,
            tactical_average_price=tactical_avg,
            realized_pnl=realized,
            tactical_realized_pnl=leg.tactical_realized_pnl,
            tactical_lots=lots,
        )

    @staticmethod
    def reduce_fill(
        leg: LegPosition,
        *,
        bucket: PositionBucket,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal = ZERO,
        tactical_lot_id: str | None = None,
        protected_core_quantity: Decimal = ZERO,
    ) -> LegPosition:
        if quantity <= ZERO or price <= ZERO or fee < ZERO:
            raise ValueError("R5.2 reduction fill is invalid")
        if quantity > leg.quantity:
            raise ValueError("R5.2 reduction exceeds open position")
        if protected_core_quantity < ZERO or protected_core_quantity > leg.core_quantity:
            raise ValueError("R5.2 protected core quantity is invalid")

        core_qty = leg.core_quantity
        core_avg = leg.core_average_price
        tactical_qty = leg.tactical_quantity
        tactical_avg = leg.tactical_average_price
        lots = list(leg.tactical_lots)
        realized_delta = -fee
        tactical_realized_delta = ZERO

        if bucket is PositionBucket.CORE:
            reducible = core_qty - protected_core_quantity
            if quantity > reducible:
                raise PermissionError("R5.2 reduction would violate core protection")
            realized_delta += _pnl(leg.side, core_avg, price, quantity)
            core_qty -= quantity
            if core_qty == ZERO:
                core_avg = ZERO
        else:
            if quantity > tactical_qty:
                raise ValueError("R5.2 tactical reduction exceeds tactical inventory")
            remaining = quantity
            selected: list[int]
            if tactical_lot_id:
                selected = [index for index, item in enumerate(lots) if item.lot_id == tactical_lot_id]
                if not selected:
                    raise KeyError(tactical_lot_id)
            else:
                selected = list(range(len(lots)))
                selected.sort(key=lambda index: (lots[index].opened_at, lots[index].layer, lots[index].lot_id))
            for index in selected:
                if remaining <= ZERO:
                    break
                lot = lots[index]
                take = min(lot.quantity, remaining)
                lot_pnl = _pnl(leg.side, lot.average_price, price, take)
                realized_delta += lot_pnl
                tactical_realized_delta += lot_pnl
                remaining -= take
                new_qty = lot.quantity - take
                lots[index] = replace(
                    lot,
                    quantity=new_qty,
                    closed_quantity=lot.closed_quantity + take,
                    realized_pnl=lot.realized_pnl + lot_pnl,
                    fees=lot.fees + (fee * take / quantity),
                )
            if remaining > ZERO:
                raise RuntimeError("R5.2 tactical lot ledger is inconsistent")
            lots = [item for item in lots if item.quantity > ZERO]
            tactical_qty -= quantity
            if tactical_qty == ZERO:
                tactical_avg = ZERO
            else:
                tactical_avg = sum((item.quantity * item.average_price for item in lots), ZERO) / tactical_qty

        total_qty = core_qty + tactical_qty
        if total_qty == ZERO:
            total_average = ZERO
        else:
            total_average = (
                (core_qty * core_avg) + (tactical_qty * tactical_avg)
            ) / total_qty
        return LegPosition(
            side=leg.side,
            quantity=total_qty,
            average_price=total_average,
            core_quantity=core_qty,
            core_average_price=core_avg,
            tactical_quantity=tactical_qty,
            tactical_average_price=tactical_avg,
            realized_pnl=leg.realized_pnl + realized_delta,
            tactical_realized_pnl=leg.tactical_realized_pnl + tactical_realized_delta,
            tactical_lots=tuple(lots),
        )

    @staticmethod
    def allocate_funding(leg: LegPosition, funding: Decimal) -> LegPosition:
        if not funding.is_finite():
            raise ValueError("R5.2 funding must be finite")
        if leg.tactical_quantity <= ZERO or not leg.tactical_lots:
            return leg
        lots = tuple(
            replace(
                lot,
                funding=lot.funding + (funding * lot.quantity / leg.tactical_quantity),
            )
            for lot in leg.tactical_lots
        )
        return replace(leg, tactical_lots=lots)
