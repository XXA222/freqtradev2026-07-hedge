
"""Persistence helpers for side-aware hedge positions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from freqtrade.enums import PositionSide
from freqtrade.exceptions import OperationalException


def normalize_position_side(value: PositionSide | str | None) -> str:
    if value is None:
        return PositionSide.BOTH.value
    if isinstance(value, PositionSide):
        return value.value
    normalized = str(value).upper()
    allowed = {item.value for item in PositionSide}
    if normalized not in allowed:
        raise ValueError(f"Unsupported position side: {value}")
    return normalized


def position_identity(
    pair: str,
    position_side: PositionSide | str | None,
) -> tuple[str, str]:
    return (pair, normalize_position_side(position_side))


def index_open_trades_by_side(trades: Iterable[Any]) -> dict[tuple[str, str], Any]:
    indexed: dict[tuple[str, str], Any] = {}
    for trade in trades:
        if not getattr(trade, "is_open", True):
            continue
        key = position_identity(
            trade.pair,
            getattr(trade, "position_side", PositionSide.BOTH.value),
        )
        if key in indexed:
            raise OperationalException(
                f"Duplicate open trade for pair={key[0]} position_side={key[1]}."
            )
        indexed[key] = trade
    return indexed


def get_open_trade_for_side(
    session: Any,
    trade_model: Any,
    pair: str,
    position_side: PositionSide | str,
) -> Any | None:
    side = normalize_position_side(position_side)
    statement = select(trade_model).where(
        trade_model.pair == pair,
        trade_model.is_open.is_(True),
        trade_model.position_side == side,
    )
    trades = list(session.scalars(statement).all())
    if len(trades) > 1:
        raise OperationalException(
            f"Duplicate open trade for pair={pair} position_side={side}."
        )
    return trades[0] if trades else None
