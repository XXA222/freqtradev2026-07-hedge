from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Mapping

from freqtrade.hedge.planning.context import (
    ActiveOrder,
    IntentAction,
    MarketSnapshot,
    OrderSide,
    OrderType,
    PositionBucket,
    PositionSide,
    TimeInForce,
)

from .mainnet_api import R5LivePreflight
from .strategy_runtime import R5StrategyFacts
from .strategy_store import R5StrategyStateStore


def _timestamp(value: object, fallback: datetime) -> datetime:
    try:
        milliseconds = int(str(value))
    except (TypeError, ValueError):
        return fallback
    if milliseconds <= 0:
        return fallback
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def _position(preflight: R5LivePreflight, symbol: str, side: str) -> tuple[Decimal, Decimal]:
    for row in preflight.managed_position_legs:
        if str(row.get("symbol", "")) == symbol and str(row.get("position_side", "")).upper() == side:
            quantity = abs(Decimal(str(row.get("position_amount", "0"))))
            average = Decimal(str(row.get("entry_price", "0")))
            return quantity, average if quantity > 0 else Decimal("0")
    return Decimal("0"), Decimal("0")


def _managed_orders(
    preflight: R5LivePreflight,
    *,
    symbol: str,
    store: R5StrategyStateStore,
    fallback_time: datetime,
) -> tuple[tuple[ActiveOrder, ...], int]:
    managed: list[ActiveOrder] = []
    external = 0
    for row in preflight.active_order_summaries:
        if str(row.get("symbol", "")) != symbol:
            external += 1
            continue
        client_id = str(row.get("client_order_id", "")).strip()
        reference = store.managed_order(client_id, account_id=preflight.account.account_id, symbol=symbol) if client_id else None
        if reference is None:
            external += 1
            continue
        payload = reference.get("payload") if isinstance(reference.get("payload"), Mapping) else {}
        try:
            action = IntentAction(str(payload.get("action", "OPEN")))
            bucket = PositionBucket(str(payload.get("bucket", "TACTICAL")))
            position_side = PositionSide(str(payload.get("position_side", row.get("position_side", ""))).upper())
            order_side = OrderSide(str(payload.get("order_side", row.get("side", ""))).upper())
            reduce_only = str(row.get("reduce_only", "false")).lower() == "true"
            managed.append(
                ActiveOrder(
                    order_id=str(row.get("order_id", client_id)),
                    client_order_id=client_id,
                    symbol=symbol,
                    position_side=position_side,
                    order_side=order_side,
                    quantity=Decimal(str(row.get("original_quantity", "0"))) - Decimal(str(row.get("executed_quantity", "0"))),
                    price=Decimal(str(row.get("price", "0"))),
                    reduce_only=reduce_only,
                    bucket=bucket,
                    action=action,
                    created_at=_timestamp(row.get("update_time"), fallback_time),
                    order_type=OrderType(str(payload.get("order_type", row.get("type", "LIMIT"))).upper()),
                    time_in_force=TimeInForce(str(payload.get("time_in_force", row.get("time_in_force", "GTC"))).upper()),
                    layer=int(payload.get("layer", 0)),
                    tactical_lot_id=(str(payload.get("tactical_lot_id")) if payload.get("tactical_lot_id") else None),
                )
            )
        except (TypeError, ValueError, KeyError):
            external += 1
    return tuple(managed), external


def facts_from_preflight(
    preflight: R5LivePreflight,
    *,
    symbol: str,
    store: R5StrategyStateStore,
    observed_at: datetime | None = None,
) -> R5StrategyFacts:
    if symbol not in {"BTCUSDT", "ETHUSDT"}:
        raise ValueError("R5.2 shadow symbol is not allowlisted")
    now = observed_at or datetime.now(UTC)
    rules = preflight.rules[symbol]
    book = preflight.books[symbol]
    long_quantity, long_average = _position(preflight, symbol, "LONG")
    short_quantity, short_average = _position(preflight, symbol, "SHORT")
    managed, external_for_symbol = _managed_orders(
        preflight,
        symbol=symbol,
        store=store,
        fallback_time=now,
    )
    leverage = Decimal(str(preflight.leverage_by_symbol.get(symbol, Decimal("1"))))
    return R5StrategyFacts(
        account_id=preflight.account.account_id,
        symbol=symbol,
        observed_at=now,
        market=MarketSnapshot(
            symbol=symbol,
            timestamp=now,
            bid=book.bid,
            ask=book.ask,
            mark=book.mark,
            tick_size=rules.tick_size,
            qty_step=rules.qty_step,
            min_qty=rules.min_qty,
            min_notional=rules.min_notional,
        ),
        balance=preflight.wallet_balance,
        equity=preflight.account.equity,
        available_balance=max(preflight.available_balance, Decimal("0")),
        leverage=leverage,
        long_quantity=long_quantity,
        long_average_price=long_average,
        short_quantity=short_quantity,
        short_average_price=short_average,
        managed_orders=managed,
        external_order_count=external_for_symbol,
        unmanaged_positions=preflight.account.unmanaged_positions,
    )
