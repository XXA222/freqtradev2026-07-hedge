from __future__ import annotations

import asyncio
import json
import socket
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .common import atomic_write_json, redact_proxy_url
from .write_lock import ExchangeWriteLocked, install as install_write_lock


TOOLKIT_VERSION = "2.3.2"
MAX_PUBLIC_JSON_BYTES = 16 * 1024 * 1024

# Spot exchangeInfo is useful for diagnosing regional/proxy behaviour, but it is
# not a runtime dependency of the futures-only R2.3 smoke configuration.  It is
# therefore reported without being allowed to veto a futures runtime that has
# passed every required REST, CCXT and WebSocket gate.
PUBLIC_ENDPOINTS = (
    ("binance_spot_exchange_info", "https://api.binance.com/api/v3/exchangeInfo", False),
    ("binance_futures_time", "https://fapi.binance.com/fapi/v1/time", True),
    ("binance_futures_exchange_info", "https://fapi.binance.com/fapi/v1/exchangeInfo", True),
)


def normalize_proxy_url(proxy_url: str) -> str:
    value = proxy_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("R2.3 currently requires an http:// or https:// proxy URL")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Proxy URL must include host and port")
    return value


def create_overlay(proxy_url: str, output_path: Path, *, db_url: str | None = None) -> dict[str, Any]:
    proxy_url = normalize_proxy_url(proxy_url)
    ccxt_common = {
        "enableRateLimit": True,
        "httpsProxy": proxy_url,
        "wsProxy": proxy_url,
    }
    overlay: dict[str, Any] = {
        "dry_run": True,
        "cancel_open_orders_on_exit": False,
        "exchange": {
            "ccxt_config": dict(ccxt_common),
            "ccxt_sync_config": dict(ccxt_common),
            "ccxt_async_config": dict(ccxt_common),
        },
        "hedge": {"read_only": True},
        "internals": {"heartbeat_interval": 10},
    }
    if db_url:
        overlay["db_url"] = db_url
    atomic_write_json(output_path, overlay)
    return overlay


def create_safe_base_config(output_path: Path) -> dict[str, Any]:
    config = {
        "$schema": "https://schema.freqtrade.io/schema_dev.json",
        "dry_run": True,
        "dry_run_wallet": 1000,
        "max_open_trades": 1,
        "stake_currency": "USDT",
        "stake_amount": 10,
        "tradable_balance_ratio": 0.99,
        "fiat_display_currency": "USD",
        "timeframe": "1m",
        "trading_mode": "futures",
        "margin_mode": "cross",
        "exchange": {
            "name": "binance",
            "key": "",
            "secret": "",
            "pair_whitelist": ["BTC/USDT:USDT"],
            "pair_blacklist": [],
        },
        "pairlists": [{"method": "StaticPairList"}],
        "entry_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "unfilledtimeout": {"entry": 10, "exit": 10, "exit_timeout_count": 0, "unit": "minutes"},
        "order_types": {
            "entry": "limit",
            "exit": "limit",
            "emergency_exit": "market",
            "force_entry": "market",
            "force_exit": "market",
            "stoploss": "market",
            "stoploss_on_exchange": False,
        },
        "order_time_in_force": {"entry": "GTC", "exit": "GTC"},
    }
    atomic_write_json(output_path, config)
    return config


def _stage(
    name: str,
    action: Callable[[], dict[str, Any]],
    *,
    required: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        details = action()
        return {
            "name": name,
            "status": "PASS",
            "required": required,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            **details,
        }
    except Exception as exc:  # noqa: BLE001 - preflight must report every external failure
        return {
            "name": name,
            "status": "FAIL" if required else "WARN",
            "required": required,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


async def _async_stage(
    name: str,
    action: Callable[[], Awaitable[dict[str, Any]]],
    *,
    required: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        details = await action()
        return {
            "name": name,
            "status": "PASS",
            "required": required,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            **details,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": name,
            "status": "FAIL" if required else "WARN",
            "required": required,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _check_proxy_socket(proxy_url: str, timeout: float) -> dict[str, Any]:
    parsed = urlsplit(proxy_url)
    with socket.create_connection((str(parsed.hostname), int(parsed.port or 0)), timeout=timeout):
        pass
    return {"proxy": redact_proxy_url(proxy_url), "tcp_connected": True}


def _fetch_json_via_proxy(url: str, proxy_url: str, timeout: float) -> dict[str, Any]:
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"freqtrade-hedge-r23-preflight/{TOOLKIT_VERSION}"},
    )
    with opener.open(request, timeout=timeout) as response:
        payload = response.read(MAX_PUBLIC_JSON_BYTES + 1)
        if len(payload) > MAX_PUBLIC_JSON_BYTES:
            raise ValueError(
                f"Public endpoint response exceeded the {MAX_PUBLIC_JSON_BYTES}-byte safety limit"
            )
        parsed = json.loads(payload)
        return {
            "url": url,
            "http_status": response.status,
            "json_type": type(parsed).__name__,
            "payload_bytes": len(payload),
        }


def _required_failures(stages: list[dict[str, Any]]) -> list[str]:
    return [
        str(stage["name"])
        for stage in stages
        if stage.get("required", True) and stage.get("status") != "PASS"
    ]


def _ccxt_sync(proxy_url: str) -> dict[str, Any]:
    import ccxt

    exchange = ccxt.binance(
        {
            "enableRateLimit": True,
            "httpsProxy": proxy_url,
            "wsProxy": proxy_url,
            "options": {"defaultType": "future"},
        }
    )
    try:
        markets = exchange.load_markets()
        futures = [symbol for symbol, market in markets.items() if market.get("swap") and market.get("linear")]
        return {"ccxt_version": ccxt.__version__, "market_count": len(markets), "linear_swap_count": len(futures)}
    finally:
        close = getattr(exchange, "close", None)
        if callable(close):
            close()


def _verify_ccxt_write_lock() -> dict[str, Any]:
    import ccxt

    exchange = ccxt.binance()
    try:
        try:
            exchange.create_order("BTC/USDT:USDT", "limit", "buy", 1, 1)
        except ExchangeWriteLocked as exc:
            return {"blocked": True, "exception": type(exc).__name__}
        raise RuntimeError("CCXT create_order was not blocked")
    finally:
        close = getattr(exchange, "close", None)
        if callable(close):
            close()


async def _ccxt_async(proxy_url: str) -> dict[str, Any]:
    import ccxt.async_support as ccxt_async

    exchange = ccxt_async.binance(
        {
            "enableRateLimit": True,
            "httpsProxy": proxy_url,
            "wsProxy": proxy_url,
            "options": {"defaultType": "future"},
        }
    )
    try:
        markets = await exchange.load_markets()
        futures = [symbol for symbol, market in markets.items() if market.get("swap") and market.get("linear")]
        return {"market_count": len(markets), "linear_swap_count": len(futures)}
    finally:
        await exchange.close()


async def _ccxt_websocket(proxy_url: str, pair: str, timeout: float) -> dict[str, Any]:
    import ccxt.pro as ccxt_pro

    exchange = ccxt_pro.binance(
        {
            "enableRateLimit": True,
            "httpsProxy": proxy_url,
            "wsProxy": proxy_url,
            "options": {"defaultType": "future"},
        }
    )
    try:
        ticker = await asyncio.wait_for(exchange.watch_ticker(pair), timeout=timeout)
        return {
            "pair": pair,
            "symbol": ticker.get("symbol"),
            "timestamp_present": ticker.get("timestamp") is not None,
            "last_present": ticker.get("last") is not None,
        }
    finally:
        await exchange.close()


async def _run_async_checks(proxy_url: str, pair: str, timeout: float, require_ws: bool) -> list[dict[str, Any]]:
    stages = [await _async_stage("ccxt_async_load_markets", lambda: _ccxt_async(proxy_url))]
    if require_ws:
        stages.append(
            await _async_stage(
                "ccxt_pro_websocket_ticker",
                lambda: _ccxt_websocket(proxy_url, pair, timeout),
            )
        )
    return stages


def run_preflight(
    proxy_url: str,
    *,
    report_path: Path,
    pair: str = "BTC/USDT:USDT",
    timeout: float = 25.0,
    require_ws: bool = True,
) -> dict[str, Any]:
    proxy_url = normalize_proxy_url(proxy_url)
    write_lock = install_write_lock()
    stages: list[dict[str, Any]] = [
        _stage("ccxt_write_lock_self_test", _verify_ccxt_write_lock),
        _stage("proxy_tcp", lambda: _check_proxy_socket(proxy_url, timeout)),
    ]
    for name, url, required in PUBLIC_ENDPOINTS:
        stages.append(
            _stage(
                name,
                lambda target=url: _fetch_json_via_proxy(target, proxy_url, timeout),
                required=required,
            )
        )
    stages.append(_stage("ccxt_sync_load_markets", lambda: _ccxt_sync(proxy_url)))
    stages.extend(asyncio.run(_run_async_checks(proxy_url, pair, timeout, require_ws)))
    required_failures = _required_failures(stages)
    optional_warnings = [
        str(stage["name"])
        for stage in stages
        if not stage.get("required", True) and stage.get("status") == "WARN"
    ]
    result = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "mode": "PUBLIC_READ_ONLY",
        "proxy": redact_proxy_url(proxy_url),
        "pair": pair,
        "exchange_writes_locked": bool(
            write_lock.get("enabled", False)
            and stages[0]["status"] == "PASS"
            and stages[0].get("blocked") is True
        ),
        "write_lock": write_lock,
        "stages": stages,
        "required_failures": required_failures,
        "optional_warnings": optional_warnings,
        "status": "PASS" if not required_failures else "FAIL",
    }
    atomic_write_json(report_path, result)
    return result
