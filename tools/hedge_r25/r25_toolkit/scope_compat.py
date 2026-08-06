from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from freqtrade.hedge.exchange import symbol_codec
from freqtrade.hedge.exchange.base import OrderFact, PositionFact, SystemClock
from freqtrade.hedge.exchange.binance_readonly import (
    AiohttpBinanceRestTransport,
    BinanceReadonlyClient,
)
from freqtrade.hedge.exchange.clock_sync import ClockSynchronizer

from .position_risk_compat import install_position_risk_compat


_DELIVERY_SYMBOL_PATTERN = r"^[A-Z0-9]{3,30}(?:_[0-9]{6})?$"
_PATCH_MARKER = "_r252_usdm_delivery_symbol_compat_installed"


def install_usdm_delivery_symbol_compat() -> dict[str, Any]:
    """Allow official USD-M delivery symbols such as ETHUSDT_261225.

    Canonical Freqtrade pairs remain unchanged. This only broadens the raw
    Binance exchange-symbol path to accept a six-digit delivery-date suffix.
    """

    if bool(getattr(symbol_codec, _PATCH_MARKER, False)):
        return {
            "installed": True,
            "already_installed": True,
            "pattern": _DELIVERY_SYMBOL_PATTERN,
        }
    previous_pattern = symbol_codec._SYMBOL_RE.pattern
    symbol_codec._SYMBOL_RE = re.compile(_DELIVERY_SYMBOL_PATTERN)
    setattr(symbol_codec, _PATCH_MARKER, True)
    return {
        "installed": True,
        "already_installed": False,
        "previous_pattern": previous_pattern,
        "pattern": _DELIVERY_SYMBOL_PATTERN,
    }


def active_observation_symbols(
    initial_symbols: Sequence[str],
    positions: Sequence[PositionFact],
    orders: Sequence[OrderFact],
) -> tuple[str, ...]:
    symbols = {str(value).strip().upper() for value in initial_symbols if str(value).strip()}
    symbols.update(item.symbol for item in positions if item.quantity != 0)
    symbols.update(item.symbol for item in orders if item.active)
    return tuple(sorted(symbols))


async def discover_active_observation_symbols(
    *,
    account_id: str,
    api_key: str,
    api_secret: str,
    initial_symbols: Sequence[str],
    proxy_url: str | None,
    request_timeout_seconds: float,
    max_clock_skew_ms: float,
) -> dict[str, Any]:
    """Discover active USD-M symbols before the main runtime is built.

    The runtime remains read-only. Active delivery contracts are added only to
    the observation/reconciliation scope so they do not appear as malformed or
    unmanaged solely because their raw Binance symbol contains a date suffix.
    """

    install_usdm_delivery_symbol_compat()
    clock = SystemClock()
    transport = AiohttpBinanceRestTransport(
        api_key=api_key,
        api_secret=api_secret,
        timeout_seconds=request_timeout_seconds,
        proxy_url=proxy_url,
        clock=clock,
    )
    client = BinanceReadonlyClient(
        transport=transport,
        account_id=account_id,
        managed_symbols=tuple(initial_symbols),
        clock_sync=ClockSynchronizer(clock=clock, max_abs_skew_ms=max_clock_skew_ms),
        clock=clock,
    )
    position_compat = install_position_risk_compat(client)
    try:
        await client.synchronize_clock()
        positions = await client.fetch_positions(None)
        orders = await client.fetch_open_orders(None)
        runtime_symbols = active_observation_symbols(initial_symbols, positions, orders)
        initial_set = {str(value).strip().upper() for value in initial_symbols}
        auto_observed = tuple(symbol for symbol in runtime_symbols if symbol not in initial_set)
        return {
            "initial_symbols": tuple(sorted(initial_set)),
            "runtime_symbols": runtime_symbols,
            "auto_observed_symbols": auto_observed,
            "active_position_symbols": tuple(
                sorted({item.symbol for item in positions if item.quantity != 0})
            ),
            "active_order_symbols": tuple(
                sorted({item.symbol for item in orders if item.active})
            ),
            "position_risk_compat": position_compat,
        }
    finally:
        await transport.close()
