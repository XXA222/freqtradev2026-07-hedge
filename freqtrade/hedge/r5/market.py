from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping

from freqtrade.hedge.execution.service import IntentAction, OrderIntent, OrderType, PositionSide

from .models import R5Stage, exact_decimal


@dataclass(frozen=True, slots=True)
class R5SymbolRules:
    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        if symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("R5 symbol rules support only BTCUSDT and ETHUSDT")
        values = {name: exact_decimal(getattr(self, name), name) for name in ("tick_size", "qty_step", "min_qty", "max_qty", "min_notional")}
        if any(value <= 0 for value in values.values()) or values["min_qty"] > values["max_qty"]:
            raise ValueError("R5 symbol rules are invalid")
        object.__setattr__(self, "symbol", symbol)
        for name, value in values.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class R5Book:
    symbol: str
    bid: Decimal
    ask: Decimal
    mark: Decimal

    def __post_init__(self) -> None:
        values = {name: exact_decimal(getattr(self, name), name) for name in ("bid", "ask", "mark")}
        if any(value <= 0 for value in values.values()) or values["bid"] > values["ask"]:
            raise ValueError("R5 market book is invalid")
        for name, value in values.items():
            object.__setattr__(self, name, value)


def quantity_for_notional(*, rules: R5SymbolRules, price: Decimal, target_notional: Decimal, maximum_notional: Decimal) -> Decimal:
    px = exact_decimal(price, "price")
    target = exact_decimal(target_notional, "target_notional")
    maximum = exact_decimal(maximum_notional, "maximum_notional")
    if target <= 0 or target > maximum:
        raise ValueError("R5 target notional is outside stage limit")
    minimum_qty = max(rules.min_qty, _ceil(rules.min_notional / px, rules.qty_step))
    maximum_qty = min(rules.max_qty, _floor(maximum / px, rules.qty_step))
    if maximum_qty < minimum_qty:
        raise ValueError("R5 symbol minimum order exceeds stage notional cap")
    target_qty = _floor(target / px, rules.qty_step)
    quantity = max(minimum_qty, min(maximum_qty, target_qty))
    if quantity <= 0 or quantity > rules.max_qty or quantity * px > maximum:
        raise ValueError("R5 symbol minimum order exceeds stage notional cap")
    return quantity


def make_limit_intent(
    *,
    account_id: str,
    stage: R5Stage,
    run_id: str,
    symbol: str,
    position_side: PositionSide,
    action: IntentAction,
    quantity: Decimal,
    price: Decimal,
    time_in_force: str,
    sequence: int,
) -> OrderIntent:
    tif = str(time_in_force).strip().upper()
    if tif not in {"GTX", "IOC", "GTC"}:
        raise ValueError("R5 supports only GTX, IOC and GTC limit orders")
    if action in {IntentAction.OPEN, IntentAction.INCREASE} and tif == "GTC":
        raise ValueError("R5 risk-increasing canary orders cannot use unbounded GTC")
    return OrderIntent(
        account_id=account_id, symbol=symbol, position_side=position_side, action=action,
        quantity=quantity, idempotency_key=f"r5:{stage.value}:{run_id}:{sequence}",
        order_type=OrderType.LIMIT, limit_price=price, reduce_only=action.reduces_risk,
        metadata={
            "time_in_force": tif,
            "self_trade_prevention_mode": "EXPIRE_BOTH",
            "r5_stage": stage.value,
            "r5_run_id": run_id,
        },
    )


def passive_price(*, book: R5Book, rules: R5SymbolRules, position_side: PositionSide, action: IntentAction) -> Decimal:
    buy = (position_side is PositionSide.LONG and not action.reduces_risk) or (position_side is PositionSide.SHORT and action.reduces_risk)
    raw = book.bid - rules.tick_size if buy else book.ask + rules.tick_size
    return _floor(raw, rules.tick_size) if buy else _ceil(raw, rules.tick_size)


def bounded_ioc_price(
    *, book: R5Book, rules: R5SymbolRules, position_side: PositionSide, action: IntentAction, max_slippage_bps: int
) -> Decimal:
    if not 0 <= max_slippage_bps <= 50:
        raise ValueError("max_slippage_bps is outside R5 hard cap")
    factor = Decimal(max_slippage_bps) / Decimal("10000")
    buy = (position_side is PositionSide.LONG and not action.reduces_risk) or (position_side is PositionSide.SHORT and action.reduces_risk)
    raw = book.ask * (Decimal("1") + factor) if buy else book.bid * (Decimal("1") - factor)
    return _ceil(raw, rules.tick_size) if buy else _floor(raw, rules.tick_size)


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
