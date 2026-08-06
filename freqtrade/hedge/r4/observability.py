from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

_SECRET_KEY = re.compile(
    r"(secret|token|password|passphrase|api[_-]?key|signature|authorization|cookie)",
    re.I,
)
_QUERY_SECRET = re.compile(
    r"(?i)(api[_-]?key|signature|token|secret|password|authorization)=([^&\s]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_METRIC_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:.-]{0,127}$")


def _redact_text(value: str) -> str:
    value = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return _BEARER.sub("Bearer [REDACTED]", value)


def redact(value: Any, *, key: str = "") -> Any:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if is_dataclass(value) and not isinstance(value, type):
        return redact(asdict(value), key=key)
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return redact(value.value, key=key)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"[BYTES:{len(value)}]"
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _atomic_write_text(path: Path, text: str, *, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: dict[str, Decimal] = defaultdict(Decimal)
        self._gauges: dict[str, Decimal] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = str(name).strip()
        if not _METRIC_NAME.fullmatch(normalized):
            raise ValueError(f"invalid metric name: {name!r}")
        return normalized

    def increment(self, name: str, amount: Decimal | int = 1) -> None:
        normalized = self._validate_name(name)
        number = Decimal(str(amount))
        if not number.is_finite() or number < 0:
            raise ValueError("counter increment must be finite and non-negative")
        with self._lock:
            if normalized in self._gauges:
                raise ValueError("metric name is already registered as a gauge")
            self._counters[normalized] += number

    def gauge(self, name: str, value: Decimal | int | float | str) -> None:
        normalized = self._validate_name(name)
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("metric value must be finite")
        with self._lock:
            if normalized in self._counters:
                raise ValueError("metric name is already registered as a counter")
            self._gauges[normalized] = number

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            merged = {**self._counters, **self._gauges}
            return {key: format(value, "f") for key, value in sorted(merged.items())}

    def write_atomic(self, path: Path) -> None:
        _atomic_write_text(
            path,
            json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )


class JsonlEventSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(self, event: str, payload: Mapping[str, Any]) -> None:
        event_name = str(event).strip()
        if not event_name:
            raise ValueError("event name cannot be empty")
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "event": event_name,
            "payload": redact(payload),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self._lock, self.path.open("a", encoding="ascii", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
