from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from freqtrade.hedge.exchange import shared_rate_limit as shared_rate_module
from freqtrade.hedge.exchange.shared_rate_limit import SqliteSharedWeightBudget

from . import TOOLKIT_VERSION


class _TrackedConnection:
    def __init__(self, connection: sqlite3.Connection, counters: dict[str, int]) -> None:
        self._connection = connection
        self._counters = counters

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._connection.__exit__(exc_type, exc, traceback)

    def close(self) -> None:
        self._counters["closed"] += 1
        self._connection.close()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    temporary.replace(path)


def _dynamic_checks() -> dict[str, bool]:
    checks: dict[str, bool] = {}
    real_connect = sqlite3.connect
    counters = {"opened": 0, "closed": 0}

    def tracked_connect(*args, **kwargs):
        counters["opened"] += 1
        return _TrackedConnection(real_connect(*args, **kwargs), counters)

    shared_rate_module.sqlite3.connect = tracked_connect
    try:
        with tempfile.TemporaryDirectory(prefix="r532-resource-closure-") as directory:
            root = Path(directory)
            path = root / "rate.sqlite"
            first = SqliteSharedWeightBudget(
                path,
                limit_per_minute=100,
                reserve_weight=10,
                now_epoch=lambda: 120.0,
            )
            second = SqliteSharedWeightBudget(
                path,
                limit_per_minute=100,
                reserve_weight=10,
                now_epoch=lambda: 120.0,
            )
            checks["first_reservation_granted"] = first.reserve_weight(60).granted is True
            denied = second.reserve_weight(31)
            checks["cross_process_reservation_denied"] = (
                denied.granted is False and denied.used_weight == 60
            )
            checks["remaining_capacity_granted"] = second.reserve_weight(30).granted is True
            checks["all_connections_closed"] = (
                counters["opened"] > 0 and counters["opened"] == counters["closed"]
            )
            path.unlink()
            checks["sqlite_file_removable"] = not path.exists()
    finally:
        shared_rate_module.sqlite3.connect = real_connect
    return checks


def run(args: argparse.Namespace) -> int:
    root = Path(args.project_root).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().resolve(strict=False)
    source = root / "freqtrade/hedge/exchange/shared_rate_limit.py"
    text = source.read_text(encoding="utf-8", errors="replace")
    checks: dict[str, bool] = {
        "source_exists": source.is_file(),
        "contextlib_closing_imported": "from contextlib import closing" in text,
        "initialize_connection_closed": "with closing(self._connect()) as connection:" in text,
        "locked_connections_closed": text.count(
            "with self._lock, closing(self._connect()) as connection:"
        ) == 2,
    }
    try:
        checks.update(_dynamic_checks())
    except Exception as exc:
        checks[f"dynamic_exception:{type(exc).__name__}:{exc}"] = False
    failed = sorted(name for name, passed in checks.items() if not passed)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "R5_3_2_RESOURCE_CLOSURE_PASS" if not failed else "R5_3_2_RESOURCE_CLOSURE_FAIL",
        "check_count": len(checks),
        "passed_count": len(checks) - len(failed),
        "failed_count": len(failed),
        "failed_checks": failed,
        "checks": checks,
        "exchange_access": "NONE",
        "real_mainnet_order_requests_sent": 0,
        "mainnet_writes": "LOCKED",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    _atomic_json(output, payload)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if not failed else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r532-offline-acceptance")
    value.add_argument("--project-root", required=True)
    value.add_argument("--output", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
