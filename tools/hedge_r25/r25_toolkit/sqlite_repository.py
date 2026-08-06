from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from freqtrade.hedge.exchange.base import (
    AccountEventFact,
    AccountSnapshotFact,
    BalanceFact,
    CalibrationKind,
    ExchangeFactBatch,
    FillFact,
    OrderFact,
    OrderOrigin,
    PositionFact,
    ReconciliationDiffFact,
    ReconciliationResolution,
    stable_fingerprint,
    to_primitive,
)
from freqtrade.hedge.integration.repository import InMemoryReadonlyRepository


def _json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_json(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("persisted fact payload must be a JSON object")
    return loaded


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _position(payload: Mapping[str, Any]) -> PositionFact:
    return PositionFact(
        account_id=str(payload["account_id"]),
        symbol=str(payload["symbol"]),
        position_side=str(payload["position_side"]),
        quantity=_decimal(payload["quantity"]),
        entry_price=_decimal(payload["entry_price"]),
        mark_price=_decimal(payload["mark_price"]),
        unrealized_pnl=_decimal(payload["unrealized_pnl"]),
        liquidation_price=(
            None
            if payload.get("liquidation_price") is None
            else _decimal(payload["liquidation_price"])
        ),
        leverage=int(payload["leverage"]),
        margin_mode=str(payload["margin_mode"]),
        update_time_ms=int(payload["update_time_ms"]),
        observed_at=_dt(str(payload["observed_at"])) or datetime.min,
        source=str(payload["source"]),
        raw=dict(payload.get("raw") or {}),
    )


def _order(payload: Mapping[str, Any]) -> OrderFact:
    origin_value = str(payload.get("origin", OrderOrigin.UNKNOWN.value))
    try:
        origin = OrderOrigin(origin_value)
    except ValueError:
        origin = OrderOrigin.UNKNOWN
    return OrderFact(
        account_id=str(payload["account_id"]),
        symbol=str(payload["symbol"]),
        position_side=str(payload["position_side"]),
        exchange_order_id=str(payload["exchange_order_id"]),
        client_order_id=str(payload["client_order_id"]),
        side=str(payload["side"]),
        order_type=str(payload["order_type"]),
        status=str(payload["status"]),
        original_quantity=_decimal(payload["original_quantity"]),
        cumulative_filled_quantity=_decimal(payload["cumulative_filled_quantity"]),
        average_price=_decimal(payload["average_price"]),
        reduce_only=bool(payload["reduce_only"]),
        update_time_ms=int(payload["update_time_ms"]),
        observed_at=_dt(str(payload["observed_at"])) or datetime.min,
        source=str(payload["source"]),
        raw=dict(payload.get("raw") or {}),
        origin=origin,
        quarantined=bool(payload.get("quarantined", False)),
        contract_version=str(payload.get("contract_version", "2.0")),
        event_version=int(payload.get("event_version", 1)),
        correlation_id=(
            None if payload.get("correlation_id") is None else str(payload["correlation_id"])
        ),
    )


def _fill(payload: Mapping[str, Any]) -> FillFact:
    return FillFact(
        account_id=str(payload["account_id"]),
        symbol=str(payload["symbol"]),
        position_side=str(payload["position_side"]),
        exchange_trade_id=str(payload["exchange_trade_id"]),
        exchange_order_id=str(payload["exchange_order_id"]),
        side=str(payload["side"]),
        quantity=_decimal(payload["quantity"]),
        price=_decimal(payload["price"]),
        commission=_decimal(payload["commission"]),
        commission_asset=(
            None if payload.get("commission_asset") is None else str(payload["commission_asset"])
        ),
        realized_pnl=_decimal(payload["realized_pnl"]),
        event_time_ms=int(payload["event_time_ms"]),
        observed_at=_dt(str(payload["observed_at"])) or datetime.min,
        source=str(payload["source"]),
        raw=dict(payload.get("raw") or {}),
    )


def _balance(payload: Mapping[str, Any]) -> BalanceFact:
    return BalanceFact(
        account_id=str(payload["account_id"]),
        asset=str(payload["asset"]),
        wallet_balance=_decimal(payload["wallet_balance"]),
        available_balance=_decimal(payload["available_balance"]),
        cross_wallet_balance=_decimal(payload["cross_wallet_balance"]),
        unrealized_pnl=_decimal(payload["unrealized_pnl"]),
        observed_at=_dt(str(payload["observed_at"])) or datetime.min,
        source=str(payload["source"]),
        raw=dict(payload.get("raw") or {}),
    )


def _account_snapshot(payload: Mapping[str, Any]) -> AccountSnapshotFact:
    return AccountSnapshotFact(
        account_id=str(payload["account_id"]),
        total_wallet_balance=_decimal(payload["total_wallet_balance"]),
        total_available_balance=_decimal(payload["total_available_balance"]),
        total_margin_balance=_decimal(payload["total_margin_balance"]),
        total_initial_margin=_decimal(payload["total_initial_margin"]),
        total_maintenance_margin=_decimal(payload["total_maintenance_margin"]),
        total_unrealized_pnl=_decimal(payload["total_unrealized_pnl"]),
        observed_at=_dt(str(payload["observed_at"])) or datetime.min,
        collection_started_at=(
            _dt(str(payload["collection_started_at"])) or datetime.min
        ),
        collection_completed_at=(
            _dt(str(payload["collection_completed_at"])) or datetime.min
        ),
        raw=dict(payload.get("raw") or {}),
    )


def _account_event(payload: Mapping[str, Any]) -> AccountEventFact:
    return AccountEventFact(
        account_id=str(payload["account_id"]),
        event_type=str(payload["event_type"]),
        event_key=str(payload["event_key"]),
        event_time_ms=int(payload["event_time_ms"]),
        transaction_time_ms=int(payload["transaction_time_ms"]),
        payload=dict(payload.get("payload") or {}),
        observed_at=_dt(str(payload["observed_at"])) or datetime.min,
        source=str(payload.get("source", "BINANCE_USER_STREAM")),
        currency=None if payload.get("currency") is None else str(payload["currency"]),
        amount=None if payload.get("amount") is None else _decimal(payload["amount"]),
        economic_event_id=(
            None
            if payload.get("economic_event_id") is None
            else str(payload["economic_event_id"])
        ),
        contract_version=str(payload.get("contract_version", "2.0")),
        event_version=int(payload.get("event_version", 1)),
        correlation_id=(
            None if payload.get("correlation_id") is None else str(payload["correlation_id"])
        ),
    )


def _diff(payload: Mapping[str, Any]) -> ReconciliationDiffFact:
    resolution_value = str(payload.get("resolution", ReconciliationResolution.OPEN.value))
    try:
        resolution = ReconciliationResolution(resolution_value)
    except ValueError:
        resolution = ReconciliationResolution.OPEN
    return ReconciliationDiffFact(
        account_id=str(payload["account_id"]),
        entity_type=str(payload["entity_type"]),
        entity_key=str(payload["entity_key"]),
        reason_code=str(payload["reason_code"]),
        expected=(None if payload.get("expected") is None else dict(payload["expected"])),
        observed=(None if payload.get("observed") is None else dict(payload["observed"])),
        severity=str(payload.get("severity", "ERROR")),
        resolution=resolution,
        resolution_detail=(
            None
            if payload.get("resolution_detail") is None
            else str(payload["resolution_detail"])
        ),
        resolved_at=(
            None if payload.get("resolved_at") is None else _dt(str(payload["resolved_at"]))
        ),
    )


class DurableReadonlyRepository(InMemoryReadonlyRepository):
    """SQLite-backed runtime repository with restart hydration and idempotent facts."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = RLock()
        self._connection = sqlite3.connect(str(self.path), timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        self._hydrate()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS r25_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS r25_fact_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_type TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    reconciliation_run_id TEXT,
                    correlation_id TEXT,
                    UNIQUE(fact_type, identity)
                );
                CREATE INDEX IF NOT EXISTS ix_r25_fact_events_account_time
                    ON r25_fact_events(account_id, observed_at);
                CREATE TABLE IF NOT EXISTS r25_latest_positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    update_time_ms INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol, position_side)
                );
                CREATE TABLE IF NOT EXISTS r25_latest_orders (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_order_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    update_time_ms INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, symbol, exchange_order_id)
                );
                CREATE TABLE IF NOT EXISTS r25_latest_balances (
                    account_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, asset)
                );
                CREATE TABLE IF NOT EXISTS r25_latest_account_snapshots (
                    account_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS r25_fills (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange_trade_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_time_ms INTEGER NOT NULL,
                    PRIMARY KEY(account_id, symbol, exchange_trade_id)
                );
                CREATE TABLE IF NOT EXISTS r25_account_events (
                    account_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_time_ms INTEGER NOT NULL,
                    PRIMARY KEY(account_id, identity)
                );
                CREATE TABLE IF NOT EXISTS r25_reconciliation_runs (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS r25_reconciliation_diffs (
                    run_id TEXT NOT NULL,
                    diff_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, diff_hash),
                    FOREIGN KEY(run_id) REFERENCES r25_reconciliation_runs(run_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS r25_history_cursors (
                    account_id TEXT NOT NULL,
                    cursor_name TEXT NOT NULL,
                    cursor_ms INTEGER NOT NULL,
                    PRIMARY KEY(account_id, cursor_name)
                );
                CREATE TABLE IF NOT EXISTS r25_observations (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS r25_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO r25_meta(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    def _hydrate(self) -> None:
        with self._db_lock:
            for row in self._connection.execute(
                "SELECT payload_json FROM r25_latest_positions"
            ):
                fact = _position(_load_json(row["payload_json"]))
                if fact.quantity != 0:
                    self._positions[fact.key] = fact
            for row in self._connection.execute(
                "SELECT payload_json FROM r25_latest_orders"
            ):
                fact = _order(_load_json(row["payload_json"]))
                self._orders[fact.key] = fact
            for row in self._connection.execute("SELECT payload_json FROM r25_fills"):
                fact = _fill(_load_json(row["payload_json"]))
                self._fills[fact.key] = fact
            for row in self._connection.execute(
                "SELECT payload_json FROM r25_account_events"
            ):
                fact = _account_event(_load_json(row["payload_json"]))
                self._account_events[(fact.account_id, fact.identity)] = fact
            for row in self._connection.execute(
                "SELECT payload_json FROM r25_latest_balances"
            ):
                fact = _balance(_load_json(row["payload_json"]))
                self._balances[(fact.account_id, fact.asset)] = fact
            for row in self._connection.execute(
                "SELECT payload_json FROM r25_latest_account_snapshots"
            ):
                fact = _account_snapshot(_load_json(row["payload_json"]))
                self._account_snapshots[fact.account_id] = fact
            for row in self._connection.execute("SELECT * FROM r25_history_cursors"):
                self._history_cursors[(row["account_id"], row["cursor_name"])] = int(
                    row["cursor_ms"]
                )
            for row in self._connection.execute("SELECT * FROM r25_reconciliation_runs"):
                self._runs[row["run_id"]] = {
                    "account_id": row["account_id"],
                    "kind": row["kind"],
                    "started_at": _dt(row["started_at"]),
                    "completed_at": _dt(row["completed_at"]),
                    "status": row["status"],
                    "reason": row["reason"],
                }
                self._diffs[row["run_id"]] = []
            for row in self._connection.execute(
                "SELECT run_id, payload_json FROM r25_reconciliation_diffs"
            ):
                self._diffs.setdefault(row["run_id"], []).append(
                    _diff(_load_json(row["payload_json"]))
                )

    @staticmethod
    def _fact_identity(fact_type: str, fact: Any) -> str:
        if fact_type == "POSITION":
            return ":".join((*fact.key, str(fact.update_time_ms), stable_fingerprint(fact)))
        if fact_type == "ORDER":
            return ":".join((*fact.key, str(fact.update_time_ms), stable_fingerprint(fact)))
        if fact_type == "FILL":
            return ":".join(fact.key)
        if fact_type == "BALANCE":
            return f"{fact.account_id}:{fact.asset}:{fact.observed_at.isoformat()}:{stable_fingerprint(fact)}"
        if fact_type == "ACCOUNT_SNAPSHOT":
            return f"{fact.account_id}:{fact.observed_at.isoformat()}:{stable_fingerprint(fact)}"
        if fact_type == "ACCOUNT_EVENT":
            return f"{fact.account_id}:{fact.identity}"
        raise ValueError(f"unsupported fact type: {fact_type}")

    def _insert_fact_event(
        self,
        connection: sqlite3.Connection,
        fact_type: str,
        fact: Any,
        *,
        reconciliation_run_id: str | None,
        correlation_id: str | None,
    ) -> None:
        payload = _json(fact)
        connection.execute(
            """
            INSERT OR IGNORE INTO r25_fact_events(
                fact_type, identity, account_id, source, observed_at,
                payload_json, payload_hash, reconciliation_run_id, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact_type,
                self._fact_identity(fact_type, fact),
                fact.account_id,
                str(getattr(fact, "source", "UNKNOWN")),
                fact.observed_at.isoformat(),
                payload,
                stable_fingerprint(fact),
                reconciliation_run_id,
                correlation_id,
            ),
        )

    def _persist_positions(
        self,
        connection: sqlite3.Connection,
        facts: Sequence[PositionFact],
        *,
        reconciliation_run_id: str | None,
        correlation_id: str | None,
    ) -> None:
        for fact in facts:
            self._insert_fact_event(
                connection,
                "POSITION",
                fact,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=correlation_id,
            )
            if fact.quantity == 0:
                connection.execute(
                    "DELETE FROM r25_latest_positions WHERE account_id=? AND symbol=? AND position_side=?",
                    fact.key,
                )
                continue
            connection.execute(
                """
                INSERT INTO r25_latest_positions(
                    account_id, symbol, position_side, payload_json,
                    update_time_ms, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, symbol, position_side) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    update_time_ms=excluded.update_time_ms,
                    observed_at=excluded.observed_at
                WHERE (excluded.update_time_ms, excluded.observed_at) >=
                      (r25_latest_positions.update_time_ms, r25_latest_positions.observed_at)
                """,
                (*fact.key, _json(fact), fact.update_time_ms, fact.observed_at.isoformat()),
            )

    def _persist_orders(
        self,
        connection: sqlite3.Connection,
        facts: Sequence[OrderFact],
        *,
        reconciliation_run_id: str | None,
        correlation_id: str | None,
    ) -> None:
        for fact in facts:
            self._insert_fact_event(
                connection,
                "ORDER",
                fact,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=correlation_id,
            )
            connection.execute(
                """
                INSERT INTO r25_latest_orders(
                    account_id, symbol, exchange_order_id, payload_json,
                    update_time_ms, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, symbol, exchange_order_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    update_time_ms=excluded.update_time_ms,
                    observed_at=excluded.observed_at
                WHERE (excluded.update_time_ms, excluded.observed_at) >=
                      (r25_latest_orders.update_time_ms, r25_latest_orders.observed_at)
                """,
                (*fact.key, _json(fact), fact.update_time_ms, fact.observed_at.isoformat()),
            )

    def _persist_fills(
        self,
        connection: sqlite3.Connection,
        facts: Sequence[FillFact],
        *,
        reconciliation_run_id: str | None,
        correlation_id: str | None,
    ) -> None:
        for fact in facts:
            self._insert_fact_event(
                connection,
                "FILL",
                fact,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=correlation_id,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO r25_fills(
                    account_id, symbol, exchange_trade_id, payload_json, event_time_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (*fact.key, _json(fact), fact.event_time_ms),
            )

    def _persist_balances(
        self,
        connection: sqlite3.Connection,
        facts: Sequence[BalanceFact],
        *,
        reconciliation_run_id: str | None,
        correlation_id: str | None,
    ) -> None:
        for fact in facts:
            self._insert_fact_event(
                connection,
                "BALANCE",
                fact,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=correlation_id,
            )
            connection.execute(
                """
                INSERT INTO r25_latest_balances(account_id, asset, payload_json, observed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, asset) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    observed_at=excluded.observed_at
                WHERE excluded.observed_at >= r25_latest_balances.observed_at
                """,
                (fact.account_id, fact.asset, _json(fact), fact.observed_at.isoformat()),
            )

    def _persist_account_snapshot(
        self,
        connection: sqlite3.Connection,
        fact: AccountSnapshotFact,
        *,
        reconciliation_run_id: str | None,
        correlation_id: str | None,
    ) -> None:
        self._insert_fact_event(
            connection,
            "ACCOUNT_SNAPSHOT",
            fact,
            reconciliation_run_id=reconciliation_run_id,
            correlation_id=correlation_id,
        )
        connection.execute(
            """
            INSERT INTO r25_latest_account_snapshots(account_id, payload_json, observed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                payload_json=excluded.payload_json,
                observed_at=excluded.observed_at
            WHERE excluded.observed_at >= r25_latest_account_snapshots.observed_at
            """,
            (fact.account_id, _json(fact), fact.observed_at.isoformat()),
        )

    def _persist_account_events(
        self,
        connection: sqlite3.Connection,
        facts: Sequence[AccountEventFact],
        *,
        reconciliation_run_id: str | None,
        correlation_id: str | None,
    ) -> None:
        for fact in facts:
            self._insert_fact_event(
                connection,
                "ACCOUNT_EVENT",
                fact,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=correlation_id,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO r25_account_events(
                    account_id, identity, payload_json, event_time_ms
                ) VALUES (?, ?, ?, ?)
                """,
                (fact.account_id, fact.identity, _json(fact), fact.event_time_ms),
            )

    def _persist_diffs(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        diffs: Sequence[ReconciliationDiffFact],
    ) -> None:
        for diff in diffs:
            payload = _json(diff)
            connection.execute(
                """
                INSERT OR IGNORE INTO r25_reconciliation_diffs(
                    run_id, diff_hash, payload_json
                ) VALUES (?, ?, ?)
                """,
                (run_id, stable_fingerprint(diff), payload),
            )

    async def append_exchange_fact_batch(self, batch: ExchangeFactBatch) -> None:
        with self._db_lock, self._connection:
            if batch.account_snapshot is not None:
                self._persist_account_snapshot(
                    self._connection,
                    batch.account_snapshot,
                    reconciliation_run_id=batch.reconciliation_run_id,
                    correlation_id=batch.correlation_id,
                )
            self._persist_balances(
                self._connection,
                batch.balances,
                reconciliation_run_id=batch.reconciliation_run_id,
                correlation_id=batch.correlation_id,
            )
            self._persist_positions(
                self._connection,
                batch.positions,
                reconciliation_run_id=batch.reconciliation_run_id,
                correlation_id=batch.correlation_id,
            )
            self._persist_orders(
                self._connection,
                batch.orders,
                reconciliation_run_id=batch.reconciliation_run_id,
                correlation_id=batch.correlation_id,
            )
            self._persist_fills(
                self._connection,
                batch.fills,
                reconciliation_run_id=batch.reconciliation_run_id,
                correlation_id=batch.correlation_id,
            )
            self._persist_account_events(
                self._connection,
                batch.account_events,
                reconciliation_run_id=batch.reconciliation_run_id,
                correlation_id=batch.correlation_id,
            )
            if batch.reconciliation_run_id is not None:
                self._persist_diffs(
                    self._connection,
                    batch.reconciliation_run_id,
                    batch.reconciliation_diffs,
                )
        with self._lock:
            self._batches.append(batch)
        if batch.account_snapshot is not None:
            await InMemoryReadonlyRepository.append_account_snapshot(
                self,
                batch.account_snapshot,
                reconciliation_run_id=batch.reconciliation_run_id,
            )
        await InMemoryReadonlyRepository.append_balance_snapshots(
            self,
            batch.balances,
            reconciliation_run_id=batch.reconciliation_run_id,
        )
        await InMemoryReadonlyRepository.append_position_snapshots(
            self,
            batch.positions,
            reconciliation_run_id=batch.reconciliation_run_id,
        )
        await InMemoryReadonlyRepository.append_order_snapshots(
            self,
            batch.orders,
            reconciliation_run_id=batch.reconciliation_run_id,
        )
        await InMemoryReadonlyRepository.append_fill_events(
            self,
            batch.fills,
            reconciliation_run_id=batch.reconciliation_run_id,
        )
        await InMemoryReadonlyRepository.append_account_events(self, batch.account_events)
        if batch.reconciliation_run_id is not None:
            await InMemoryReadonlyRepository.append_reconciliation_diffs(
                self,
                batch.reconciliation_run_id,
                batch.reconciliation_diffs,
            )

    async def append_position_snapshots(
        self,
        facts: Sequence[PositionFact],
        *,
        reconciliation_run_id: str | None = None,
    ) -> None:
        with self._db_lock, self._connection:
            self._persist_positions(
                self._connection,
                facts,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=None,
            )
        await InMemoryReadonlyRepository.append_position_snapshots(
            self, facts, reconciliation_run_id=reconciliation_run_id
        )

    async def append_order_snapshots(
        self,
        facts: Sequence[OrderFact],
        *,
        reconciliation_run_id: str | None = None,
    ) -> None:
        with self._db_lock, self._connection:
            self._persist_orders(
                self._connection,
                facts,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=None,
            )
        await InMemoryReadonlyRepository.append_order_snapshots(
            self, facts, reconciliation_run_id=reconciliation_run_id
        )

    async def append_fill_events(
        self,
        facts: Sequence[FillFact],
        *,
        reconciliation_run_id: str | None = None,
    ) -> None:
        with self._db_lock, self._connection:
            self._persist_fills(
                self._connection,
                facts,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=None,
            )
        await InMemoryReadonlyRepository.append_fill_events(
            self, facts, reconciliation_run_id=reconciliation_run_id
        )

    async def append_account_snapshot(
        self,
        fact: AccountSnapshotFact,
        *,
        reconciliation_run_id: str | None = None,
    ) -> None:
        with self._db_lock, self._connection:
            self._persist_account_snapshot(
                self._connection,
                fact,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=None,
            )
        await InMemoryReadonlyRepository.append_account_snapshot(
            self, fact, reconciliation_run_id=reconciliation_run_id
        )

    async def append_balance_snapshots(
        self,
        facts: Sequence[BalanceFact],
        *,
        reconciliation_run_id: str | None = None,
    ) -> None:
        with self._db_lock, self._connection:
            self._persist_balances(
                self._connection,
                facts,
                reconciliation_run_id=reconciliation_run_id,
                correlation_id=None,
            )
        await InMemoryReadonlyRepository.append_balance_snapshots(
            self, facts, reconciliation_run_id=reconciliation_run_id
        )

    async def append_account_events(self, facts: Sequence[AccountEventFact]) -> None:
        with self._db_lock, self._connection:
            self._persist_account_events(
                self._connection,
                facts,
                reconciliation_run_id=None,
                correlation_id=None,
            )
        await InMemoryReadonlyRepository.append_account_events(self, facts)

    async def begin_reconciliation(
        self,
        *,
        account_id: str,
        kind: CalibrationKind,
        started_at: datetime,
    ) -> str:
        run_id = uuid4().hex
        with self._db_lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO r25_reconciliation_runs(
                    run_id, account_id, kind, started_at, completed_at, status, reason
                ) VALUES (?, ?, ?, ?, NULL, 'RUNNING', '')
                """,
                (run_id, account_id, kind.value, started_at.isoformat()),
            )
        with self._lock:
            self._runs[run_id] = {
                "account_id": account_id,
                "kind": kind.value,
                "started_at": started_at,
                "completed_at": None,
                "status": "RUNNING",
                "reason": "",
            }
            self._diffs[run_id] = []
        return run_id

    async def append_reconciliation_diffs(
        self,
        run_id: str,
        diffs: Sequence[ReconciliationDiffFact],
    ) -> None:
        with self._db_lock, self._connection:
            self._persist_diffs(self._connection, run_id, diffs)
        await InMemoryReadonlyRepository.append_reconciliation_diffs(self, run_id, diffs)

    async def complete_reconciliation(
        self,
        run_id: str,
        *,
        completed_at: datetime,
        status: str,
        reason: str,
    ) -> None:
        with self._db_lock, self._connection:
            self._connection.execute(
                """
                UPDATE r25_reconciliation_runs
                SET completed_at=?, status=?, reason=?
                WHERE run_id=?
                """,
                (completed_at.isoformat(), status, reason, run_id),
            )
        await InMemoryReadonlyRepository.complete_reconciliation(
            self,
            run_id,
            completed_at=completed_at,
            status=status,
            reason=reason,
        )

    async def save_history_cursor(
        self, account_id: str, cursor_name: str, cursor_ms: int
    ) -> None:
        parsed = int(cursor_ms)
        with self._db_lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO r25_history_cursors(account_id, cursor_name, cursor_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id, cursor_name) DO UPDATE SET
                    cursor_ms=MAX(r25_history_cursors.cursor_ms, excluded.cursor_ms)
                """,
                (account_id, cursor_name, parsed),
            )
        await InMemoryReadonlyRepository.save_history_cursor(
            self, account_id, cursor_name, parsed
        )

    def start_run(self, run_id: str, started_at: datetime, payload: Mapping[str, Any]) -> None:
        with self._db_lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO r25_runs(run_id, started_at, completed_at, status, payload_json)
                VALUES (?, ?, NULL, 'RUNNING', ?)
                """,
                (run_id, started_at.isoformat(), _json(dict(payload))),
            )

    def complete_run(
        self,
        run_id: str,
        *,
        completed_at: datetime,
        status: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self._db_lock, self._connection:
            self._connection.execute(
                """
                UPDATE r25_runs SET completed_at=?, status=?, payload_json=? WHERE run_id=?
                """,
                (completed_at.isoformat(), status, _json(dict(payload)), run_id),
            )

    def record_observation(
        self,
        run_id: str,
        sequence: int,
        observed_at: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        serialized = _json(dict(payload))
        with self._db_lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO r25_observations(
                    run_id, sequence, observed_at, payload_json, payload_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(sequence),
                    observed_at.isoformat(),
                    serialized,
                    stable_fingerprint(dict(payload)),
                ),
            )

    def counts(self) -> dict[str, int]:
        names = (
            "r25_fact_events",
            "r25_latest_positions",
            "r25_latest_orders",
            "r25_latest_balances",
            "r25_latest_account_snapshots",
            "r25_fills",
            "r25_account_events",
            "r25_reconciliation_runs",
            "r25_reconciliation_diffs",
            "r25_history_cursors",
            "r25_observations",
            "r25_runs",
        )
        with self._db_lock:
            return {
                name: int(self._connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in names
            }

    def integrity(self) -> dict[str, Any]:
        with self._db_lock:
            integrity = str(self._connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = list(self._connection.execute("PRAGMA foreign_key_check"))
            quick = str(self._connection.execute("PRAGMA quick_check").fetchone()[0])
        return {
            "integrity_check": integrity,
            "quick_check": quick,
            "foreign_key_violation_count": len(foreign_keys),
            "status": (
                "PASS"
                if integrity == "ok" and quick == "ok" and not foreign_keys
                else "FAIL"
            ),
        }

    def event_source_counts(self) -> dict[str, int]:
        with self._db_lock:
            rows = self._connection.execute(
                "SELECT source, COUNT(*) AS count FROM r25_fact_events GROUP BY source"
            ).fetchall()
        return {str(row["source"]): int(row["count"]) for row in rows}

    def close(self) -> None:
        with self._db_lock:
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._connection.close()
