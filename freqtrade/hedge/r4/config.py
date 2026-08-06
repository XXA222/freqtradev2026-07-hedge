from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .models import R4Mode, R4RuntimeConfig

_ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
_ACCOUNT_ID = re.compile(r"^(binance-usdm|binance-usdm-testnet):[A-Za-z0-9._-]{1,128}$")


def _path(root: Path, value: object | None) -> Path | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("path values cannot be booleans")
    text = str(value).strip()
    if not text or "\x00" in text:
        raise ValueError("path value is invalid")
    result = Path(text).expanduser()
    return (result if result.is_absolute() else root / result).resolve(strict=False)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def _positive_int(value: object, name: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isascii() or not text.isdigit():
            raise ValueError(f"{name} must be an integer")
        result = int(text)
    elif isinstance(value, int):
        result = value
    else:
        raise ValueError(f"{name} must be an integer")
    if not 0 < result <= maximum:
        raise ValueError(f"{name} must be in [1, {maximum}]")
    return result


def _normalize_symbol(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("symbol cannot be a boolean")
    text = str(value).strip().upper()
    aliases = {
        "BTCUSDT": "BTCUSDT",
        "BTC/USDT": "BTCUSDT",
        "BTC/USDT:USDT": "BTCUSDT",
        "BTCUSDT:USDT": "BTCUSDT",
        "ETHUSDT": "ETHUSDT",
        "ETH/USDT": "ETHUSDT",
        "ETH/USDT:USDT": "ETHUSDT",
        "ETHUSDT:USDT": "ETHUSDT",
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError("R4 supports only BTCUSDT and ETHUSDT USD-M perpetual symbols") from exc


def runtime_config_from_mapping(values: Mapping[str, Any], *, project_root: Path) -> R4RuntimeConfig:
    if not isinstance(values, Mapping):
        raise ValueError("R4 configuration must be an object")
    section = values.get("hedge_r4", values)
    if not isinstance(section, Mapping):
        raise ValueError("hedge_r4 must be an object")
    try:
        mode = R4Mode(str(section.get("mode", R4Mode.PRODUCTION_LOCKED.value)).strip().upper())
    except ValueError as exc:
        raise ValueError("mode must be DRY_RUN, TESTNET, or PRODUCTION_LOCKED") from exc

    raw_symbols = section.get("symbols", ("BTCUSDT", "ETHUSDT"))
    if isinstance(raw_symbols, (str, bytes)) or not isinstance(raw_symbols, (list, tuple)):
        raise ValueError("symbols must be an array")
    symbols = tuple(_normalize_symbol(item) for item in raw_symbols)
    if not symbols:
        raise ValueError("at least one R4 symbol is required")
    if len(set(symbols)) != len(symbols):
        raise ValueError("R4 symbols must be unique")

    mainnet_writes = _bool(
        section.get("mainnet_writes_enabled", False), "mainnet_writes_enabled"
    )
    if mainnet_writes:
        raise ValueError("R4 mainnet writes are permanently locked")
    allow_market = _bool(section.get("allow_market_orders", False), "allow_market_orders")
    if allow_market:
        raise ValueError("R4 market orders are disabled")

    max_notional = _decimal(
        section.get("max_single_order_notional", "25"), "max_single_order_notional"
    )
    gross = _decimal(
        section.get("max_gross_wallet_exposure", "0.65"), "max_gross_wallet_exposure"
    )
    if max_notional <= 0:
        raise ValueError("max_single_order_notional must be positive")
    if not Decimal("0") < gross <= Decimal("1"):
        raise ValueError("max_gross_wallet_exposure must be in (0, 1]")

    heartbeat = _positive_int(
        section.get("heartbeat_seconds", 5), "heartbeat_seconds", maximum=3600
    )
    reconciliation_age = _positive_int(
        section.get("reconciliation_max_age_seconds", 30),
        "reconciliation_max_age_seconds",
        maximum=86400,
    )
    stream_age = _positive_int(
        section.get("user_stream_max_age_seconds", 30),
        "user_stream_max_age_seconds",
        maximum=86400,
    )
    market_age = _positive_int(
        section.get("market_data_max_age_seconds", 120),
        "market_data_max_age_seconds",
        maximum=86400,
    )

    account_id = str(section.get("account_id", "binance-usdm:default")).strip()
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("account_id must use a binance-usdm or binance-usdm-testnet namespace")
    if mode is R4Mode.TESTNET and not account_id.startswith("binance-usdm-testnet:"):
        raise ValueError("TESTNET requires binance-usdm-testnet account namespace")
    if mode is not R4Mode.TESTNET and account_id.startswith("binance-usdm-testnet:"):
        raise ValueError(f"{mode.value} cannot use a Testnet account namespace")

    root = project_root.expanduser().resolve(strict=False)
    return R4RuntimeConfig(
        project_root=root,
        mode=mode,
        account_id=account_id,
        symbols=symbols,
        state_dir=_path(root, section.get("state_dir")),
        database_path=_path(root, section.get("database_path")),
        config_path=_path(root, section.get("config_path")),
        metrics_path=_path(root, section.get("metrics_path")),
        events_path=_path(root, section.get("events_path")),
        heartbeat_seconds=heartbeat,
        reconciliation_max_age_seconds=reconciliation_age,
        user_stream_max_age_seconds=stream_age,
        market_data_max_age_seconds=market_age,
        planner_enabled=_bool(section.get("planner_enabled", True), "planner_enabled"),
        control_plane_enabled=_bool(
            section.get("control_plane_enabled", True), "control_plane_enabled"
        ),
        testnet_validation_only=_bool(
            section.get("testnet_validation_only", True), "testnet_validation_only"
        ),
        mainnet_writes_enabled=False,
        allow_market_orders=False,
        max_single_order_notional=max_notional,
        max_gross_wallet_exposure=gross,
    )


def load_runtime_config(path: Path, *, project_root: Path | None = None) -> R4RuntimeConfig:
    resolved = path.expanduser().resolve(strict=True)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid R4 JSON configuration at line {exc.lineno}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("R4 configuration root must be an object")
    return runtime_config_from_mapping(payload, project_root=project_root or resolved.parent)
