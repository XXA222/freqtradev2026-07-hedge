from __future__ import annotations

import sqlite3
from pathlib import Path

from freqtrade.hedge.exchange import shared_rate_limit as shared_rate_module
from freqtrade.hedge.exchange.shared_rate_limit import SqliteSharedWeightBudget


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


def test_every_shared_rate_sqlite_connection_is_explicitly_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    real_connect = sqlite3.connect
    counters = {"opened": 0, "closed": 0}

    def tracked_connect(*args, **kwargs):
        counters["opened"] += 1
        return _TrackedConnection(real_connect(*args, **kwargs), counters)

    monkeypatch.setattr(shared_rate_module.sqlite3, "connect", tracked_connect)
    budget = SqliteSharedWeightBudget(
        tmp_path / "rate.sqlite",
        limit_per_minute=100,
        reserve_weight=10,
        now_epoch=lambda: 120.0,
    )
    assert budget.reserve_weight(60).granted is True
    assert budget.observe_remote_weight(70) == 70
    assert budget.snapshot().used_weight == 70
    assert counters["opened"] == 4
    assert counters["closed"] == counters["opened"]


def test_shared_rate_sqlite_file_can_be_removed_after_operations(tmp_path: Path) -> None:
    path = tmp_path / "rate.sqlite"
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
    assert first.reserve_weight(60).granted is True
    assert second.reserve_weight(31).granted is False
    assert second.reserve_weight(30).granted is True
    path.unlink()
    assert not path.exists()
