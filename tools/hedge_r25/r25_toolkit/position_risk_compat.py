from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MethodType
from typing import Any

from freqtrade.hedge.exchange.binance_normalizer import normalize_positions
from freqtrade.hedge.exchange.rate_limit import BinanceDataError, BinanceTransportError
from freqtrade.hedge.exchange.symbol_codec import to_binance_symbol


_PATCH_MARKER = "_r25_position_risk_compat_installed"


def _configuration_by_symbol(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in rows:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        result[symbol] = item
    return result


def _enrich_position_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    configuration_by_symbol: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    enriched: list[Mapping[str, Any]] = []
    for item in rows:
        symbol = str(item.get("symbol") or item.get("s") or "").strip().upper()
        configuration = configuration_by_symbol.get(symbol)
        if configuration is None:
            enriched.append(item)
            continue
        mutable = dict(item)
        if mutable.get("marginType") in (None, "") and "isolated" not in mutable:
            margin_type = configuration.get("marginType")
            if margin_type not in (None, ""):
                mutable["marginType"] = margin_type
        if mutable.get("leverage") in (None, ""):
            leverage = configuration.get("leverage")
            if leverage not in (None, ""):
                mutable["leverage"] = leverage
        enriched.append(mutable)
    return tuple(enriched)


async def _fetch_positions_with_symbol_configuration(
    self: Any,
    symbols: Sequence[str] | None = None,
):
    params: dict[str, Any] = {}
    if symbols is not None:
        if len(symbols) != 1:
            raise ValueError("Binance positionRisk requires exactly one symbol or None")
        params["symbol"] = to_binance_symbol(str(symbols[0]))

    configuration_rows = await self.fetch_symbol_configuration(None)
    configuration_by_symbol = _configuration_by_symbol(configuration_rows)

    for path in ("/fapi/v3/positionRisk", "/fapi/v2/positionRisk"):
        try:
            response = await self.transport.request(
                "GET", path, params=params, signed=True, weight=5
            )
            data = response.data
            if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
                raise BinanceDataError(f"{path} returned invalid data")
            object_rows = tuple(item for item in data if isinstance(item, Mapping))
            if len(object_rows) != len(data):
                raise BinanceDataError(f"{path} contains a non-object item")
            enriched = _enrich_position_rows(
                object_rows,
                configuration_by_symbol=configuration_by_symbol,
            )
            return normalize_positions(
                enriched,
                account_id=self.account_id,
                observed_at=self.clock.now(),
            )
        except BinanceTransportError as exc:
            if exc.status == 404:
                continue
            raise
    raise BinanceDataError("No supported Binance position endpoint succeeded")


def install_position_risk_compat(client: Any) -> dict[str, Any]:
    """Enrich USD-M Position V3 rows with symbolConfig margin mode and leverage.

    Binance Position V3 may omit marginType/isolated while symbolConfig remains
    authoritative. The core normalizer intentionally requires a margin mode, so
    R2.5 binds a compatibility fetcher to the live readonly client before startup.
    """

    if bool(getattr(client, _PATCH_MARKER, False)):
        return {
            "installed": True,
            "already_installed": True,
            "source": "symbolConfig",
            "fallback_position_endpoint": "/fapi/v2/positionRisk",
        }
    client.fetch_positions = MethodType(
        _fetch_positions_with_symbol_configuration,
        client,
    )
    setattr(client, _PATCH_MARKER, True)
    return {
        "installed": True,
        "already_installed": False,
        "source": "symbolConfig",
        "fallback_position_endpoint": "/fapi/v2/positionRisk",
    }
