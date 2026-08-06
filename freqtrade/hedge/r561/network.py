"""Public Binance/CCXT network probe and deterministic data-quality evaluation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class ProbeAttempt:
    sequence: int
    ok: bool
    latency_ms: int
    observed_at: datetime
    candle_timestamp: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkProbeReport:
    ready: bool
    attempted: int
    succeeded: int
    failed: int
    success_ratio: Decimal
    max_latency_ms: int
    last_candle_age_seconds: int | None
    missing_candles: int
    reasons: tuple[str, ...]
    attempts: tuple[ProbeAttempt, ...]


def evaluate_probe_attempts(
    attempts: Iterable[ProbeAttempt],
    *,
    required_attempts: int,
    minimum_success_ratio: Decimal = Decimal("1"),
    max_last_candle_age_seconds: int = 90,
    max_missing_candles: int = 0,
    timeframe_seconds: int = 60,
    now: datetime | None = None,
) -> NetworkProbeReport:
    rows = tuple(attempts)
    attempted = len(rows)
    succeeded = sum(1 for item in rows if item.ok)
    failed = attempted - succeeded
    ratio = Decimal(succeeded) / Decimal(attempted) if attempted else Decimal("0")
    reasons: list[str] = []
    if attempted < required_attempts:
        reasons.append(f"NETWORK_ATTEMPTS_INSUFFICIENT:{attempted}/{required_attempts}")
    if ratio < minimum_success_ratio:
        reasons.append(f"NETWORK_SUCCESS_RATIO_LOW:{ratio}")
    candles = sorted(
        item.candle_timestamp for item in rows if item.ok and item.candle_timestamp is not None
    )
    missing = 0
    for previous, current in zip(candles, candles[1:], strict=False):
        delta = int((current - previous).total_seconds())
        if delta > timeframe_seconds:
            missing += max(delta // timeframe_seconds - 1, 0)
        elif delta < 0:
            reasons.append("NETWORK_CANDLE_OUT_OF_ORDER")
    if missing > max_missing_candles:
        reasons.append(f"NETWORK_CANDLE_GAPS:{missing}")
    age: int | None = None
    if candles:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        age = max(0, int((reference - candles[-1]).total_seconds()))
        if age > max_last_candle_age_seconds:
            reasons.append(f"NETWORK_LAST_CANDLE_STALE:{age}")
    elif any(item.candle_timestamp is not None for item in rows):
        reasons.append("NETWORK_CANDLE_UNAVAILABLE")
    max_latency = max((item.latency_ms for item in rows), default=0)
    return NetworkProbeReport(
        ready=not reasons,
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        success_ratio=ratio,
        max_latency_ms=max_latency,
        last_candle_age_seconds=age,
        missing_candles=missing,
        reasons=tuple(dict.fromkeys(reasons)),
        attempts=rows,
    )


def _aware_timestamp(milliseconds: object) -> datetime | None:
    if milliseconds is None:
        return None
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def run_sync_ticker_probe(
    config: Mapping[str, Any],
    *,
    symbol: str,
    attempts: int = 20,
) -> tuple[ProbeAttempt, ...]:
    import ccxt

    exchange_cfg = config.get("exchange", {})
    raw = exchange_cfg.get("ccxt_config", {}) if isinstance(exchange_cfg, Mapping) else {}
    constructor = dict(raw) if isinstance(raw, Mapping) else {}
    constructor.setdefault("enableRateLimit", True)
    constructor.setdefault("options", {"defaultType": "swap"})
    exchange = ccxt.binance(constructor)
    rows: list[ProbeAttempt] = []
    try:
        for sequence in range(1, attempts + 1):
            started = monotonic()
            try:
                ticker = exchange.fetch_ticker(symbol)
                rows.append(
                    ProbeAttempt(
                        sequence,
                        True,
                        int((monotonic() - started) * 1000),
                        datetime.now(UTC),
                        _aware_timestamp(ticker.get("timestamp")),
                    )
                )
            except Exception as exc:
                rows.append(
                    ProbeAttempt(
                        sequence,
                        False,
                        int((monotonic() - started) * 1000),
                        datetime.now(UTC),
                        error=f"{type(exc).__name__}:{exc}"[:512],
                    )
                )
    finally:
        close = getattr(exchange, "close", None)
        if callable(close):
            close()
    return tuple(rows)


async def run_async_ohlcv_probe(
    config: Mapping[str, Any],
    *,
    symbol: str,
    timeframe: str = "1m",
    attempts: int = 20,
) -> tuple[ProbeAttempt, ...]:
    import ccxt.async_support as ccxt_async

    exchange_cfg = config.get("exchange", {})
    raw_sync = exchange_cfg.get("ccxt_config", {}) if isinstance(exchange_cfg, Mapping) else {}
    raw_async = exchange_cfg.get("ccxt_async_config", {}) if isinstance(exchange_cfg, Mapping) else {}
    constructor = dict(raw_sync) if isinstance(raw_sync, Mapping) else {}
    if isinstance(raw_async, Mapping):
        constructor.update(raw_async)
    constructor.setdefault("enableRateLimit", True)
    constructor.setdefault("options", {"defaultType": "swap"})
    exchange = ccxt_async.binance(constructor)
    rows: list[ProbeAttempt] = []
    try:
        for sequence in range(1, attempts + 1):
            started = monotonic()
            try:
                candles = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=2)
                timestamp = _aware_timestamp(candles[-1][0]) if candles else None
                rows.append(
                    ProbeAttempt(
                        sequence,
                        True,
                        int((monotonic() - started) * 1000),
                        datetime.now(UTC),
                        timestamp,
                    )
                )
            except Exception as exc:
                rows.append(
                    ProbeAttempt(
                        sequence,
                        False,
                        int((monotonic() - started) * 1000),
                        datetime.now(UTC),
                        error=f"{type(exc).__name__}:{exc}"[:512],
                    )
                )
    finally:
        await exchange.close()
    return tuple(rows)


def run_public_network_probe(
    config: Mapping[str, Any],
    *,
    symbol: str,
    attempts: int = 20,
) -> dict[str, NetworkProbeReport]:
    sync_rows = run_sync_ticker_probe(config, symbol=symbol, attempts=attempts)
    async_rows = asyncio.run(
        run_async_ohlcv_probe(config, symbol=symbol, attempts=attempts)
    )
    return {
        "ticker": evaluate_probe_attempts(
            sync_rows,
            required_attempts=attempts,
            max_last_candle_age_seconds=300,
        ),
        "ohlcv": evaluate_probe_attempts(
            async_rows,
            required_attempts=attempts,
            max_last_candle_age_seconds=90,
        ),
    }
