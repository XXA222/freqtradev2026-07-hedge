from __future__ import annotations

import hashlib
import json
import math
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SECRET_FIELD_RE = re.compile(
    r"(?i)(api[-_]?key|secret|signature|authorization|x-mbx-apikey|password|token)"
)
SENSITIVE_QUERY_FIELDS = frozenset({"signature"})


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, canonical_json(value, pretty=True) + "\n")


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=True,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    query = []
    for name, raw_value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((name, "***" if name.lower() in SENSITIVE_QUERY_FIELDS else raw_value))
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.username or parts.password:
        host = f"***:***@{host}"
    return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))


def redact_object(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***" if SECRET_FIELD_RE.search(str(key)) else redact_object(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_object(item) for item in value]
    if isinstance(value, tuple):
        return [redact_object(item) for item in value]
    return value


def decimal_value(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal string")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{field} is not finite: {value!r}")
    return result


def assert_no_nonfinite(value: Any, *, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Non-finite number at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_nonfinite(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_nonfinite(item, path=f"{path}[{index}]")


def chunks_by_time(start_ms: int, end_ms: int, *, max_window_ms: int) -> Iterable[tuple[int, int]]:
    if start_ms > end_ms:
        return
    cursor = start_ms
    while cursor <= end_ms:
        window_end = min(end_ms, cursor + max_window_ms - 1)
        yield cursor, window_end
        cursor = window_end + 1
