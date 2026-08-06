from __future__ import annotations

import functools
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_METHOD_NAMES = (
    "add_margin",
    "borrow_margin",
    "cancel_all_orders",
    "cancel_order",
    "cancel_orders",
    "create_deposit_address",
    "create_order",
    "create_orders",
    "edit_order",
    "reduce_margin",
    "repay_margin",
    "set_leverage",
    "set_margin_mode",
    "set_position_mode",
    "transfer",
    "withdraw",
)
_LOCK = threading.Lock()


class ExchangeWriteLocked(RuntimeError):
    """Raised when R2.4 prevents a state-changing exchange operation."""


def is_read_only_http_method(method: object) -> bool:
    return str(method or "GET").upper() in SAFE_HTTP_METHODS


def _audit(event: dict[str, Any]) -> None:
    path_value = os.environ.get("FREQTRADE_HEDGE_WRITE_LOCK_AUDIT", "").strip()
    if not path_value:
        return
    destination = Path(path_value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "event": "exchange_write_blocked",
        **event,
    }
    with _LOCK, destination.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _blocked_operation(name: str) -> Callable[..., Any]:
    def blocked(self: Any, *args: Any, **kwargs: Any) -> Any:
        exchange = getattr(self, "id", self.__class__.__name__)
        _audit({"exchange": str(exchange), "operation": name})
        raise ExchangeWriteLocked(f"Exchange write is locked by R2.4: {exchange}.{name}")

    blocked.__name__ = name
    blocked.__qualname__ = name
    setattr(blocked, "__r23_write_lock__", True)
    return blocked


def _guard_http_method(original: Callable[..., Any], method_index: int, operation: str) -> Callable[..., Any]:
    if getattr(original, "__r23_write_lock__", False):
        return original

    @functools.wraps(original)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        method: object = kwargs.get("method", "GET")
        if "method" not in kwargs and len(args) > method_index:
            method = args[method_index]
        if not is_read_only_http_method(method):
            exchange = getattr(self, "id", self.__class__.__name__)
            _audit({"exchange": str(exchange), "operation": operation, "http_method": str(method)})
            raise ExchangeWriteLocked(
                f"Exchange HTTP write is locked by R2.4: {exchange} {str(method).upper()}"
            )
        return original(self, *args, **kwargs)

    setattr(guarded, "__r23_write_lock__", True)
    return guarded


def _patch_class(exchange_class: type[Any]) -> int:
    changed = 0
    for name in WRITE_METHOD_NAMES:
        if hasattr(exchange_class, name):
            setattr(exchange_class, name, _blocked_operation(name))
            changed += 1
    for name, method_index in (("fetch", 1), ("fetch2", 2), ("request", 2)):
        original = getattr(exchange_class, name, None)
        if callable(original) and not getattr(original, "__r23_write_lock__", False):
            setattr(exchange_class, name, _guard_http_method(original, method_index, name))
            changed += 1
    return changed


def install() -> dict[str, Any]:
    if os.environ.get("FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK", "0") != "1":
        return {"enabled": False, "patched_classes": 0, "patched_methods": 0}

    classes: list[type[Any]] = []
    try:
        import ccxt

        classes.extend([ccxt.Exchange, ccxt.binance])
    except (ImportError, AttributeError):
        pass
    try:
        import ccxt.async_support as ccxt_async

        classes.extend([ccxt_async.Exchange, ccxt_async.binance])
    except (ImportError, AttributeError):
        pass
    try:
        import ccxt.pro as ccxt_pro

        classes.extend([ccxt_pro.Exchange, ccxt_pro.binance])
    except (ImportError, AttributeError):
        pass

    unique_classes: list[type[Any]] = []
    seen: set[int] = set()
    for exchange_class in classes:
        if id(exchange_class) not in seen:
            unique_classes.append(exchange_class)
            seen.add(id(exchange_class))
    changed = sum(_patch_class(exchange_class) for exchange_class in unique_classes)
    return {"enabled": True, "patched_classes": len(unique_classes), "patched_methods": changed}
