from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import canonical_json, sha256_bytes, sha256_json


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS readonly_snapshot_runs (
    run_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    toolkit_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    entity_hash TEXT NOT NULL,
    entity_count INTEGER NOT NULL,
    start_server_ms INTEGER NOT NULL,
    end_server_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS readonly_snapshot_entities (
    run_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    symbol TEXT,
    position_side TEXT,
    event_time_ms INTEGER,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, entity_type, natural_key),
    FOREIGN KEY (run_id) REFERENCES readonly_snapshot_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_readonly_entities_symbol_side
    ON readonly_snapshot_entities(entity_type, symbol, position_side);
CREATE INDEX IF NOT EXISTS ix_readonly_entities_event_time
    ON readonly_snapshot_entities(entity_type, event_time_ms);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return connection


def _row(
    entity_type: str,
    natural_key: str,
    payload: Any,
    *,
    symbol: str | None = None,
    position_side: str | None = None,
    event_time_ms: int | None = None,
) -> dict[str, Any]:
    payload_json = canonical_json(payload)
    return {
        "entity_type": entity_type,
        "natural_key": natural_key,
        "symbol": symbol,
        "position_side": position_side,
        "event_time_ms": event_time_ms,
        "payload_json": payload_json,
        "payload_sha256": sha256_bytes(payload_json.encode("utf-8")),
    }


def _list_rows(
    entity_type: str,
    values: Iterable[Mapping[str, Any]],
    key_fields: tuple[str, ...],
    *,
    time_fields: tuple[str, ...] = (),
) -> Iterable[dict[str, Any]]:
    for value in values:
        natural_key = ":".join(str(value.get(field, "")) for field in key_fields)
        event_time = None
        for field in time_fields:
            if value.get(field) is not None:
                event_time = int(value[field])
                break
        yield _row(
            entity_type,
            natural_key,
            value,
            symbol=str(value.get("symbol")) if value.get("symbol") else None,
            position_side=(
                str(value.get("positionSide")) if value.get("positionSide") else None
            ),
            event_time_ms=event_time,
        )


def snapshot_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [
        _row("metadata", "scope", snapshot["scope"]),
        _row("metadata", "clock", snapshot["clock"]),
        _row("metadata", "snapshot_interval", snapshot["snapshot_interval"]),
        _row("metadata", "permissions", snapshot["permissions"]),
        _row("metadata", "key_policy", snapshot["key_policy"]),
        _row("metadata", "trading_status", snapshot["trading_status"]),
        _row("metadata", "modes", snapshot["modes"]),
        _row("metadata", "account", snapshot["account"]),
        _row("metadata", "transport", snapshot["transport"]),
        _row("metadata", "optional_endpoint_errors", snapshot["optional_endpoint_errors"]),
    ]
    rows.extend(_list_rows("balance", snapshot["balances"], ("asset",), time_fields=("updateTime",)))
    rows.extend(
        _list_rows(
            "position",
            snapshot["positions"],
            ("symbol", "positionSide"),
            time_fields=("updateTime",),
        )
    )
    rows.extend(
        _list_rows(
            "open_order",
            snapshot["open_orders"],
            ("symbol", "orderId"),
            time_fields=("updateTime", "time"),
        )
    )
    rows.extend(
        _list_rows(
            "order",
            snapshot["orders"],
            ("symbol", "orderId"),
            time_fields=("updateTime", "time"),
        )
    )
    rows.extend(
        _list_rows(
            "trade",
            snapshot["trades"],
            ("symbol", "id"),
            time_fields=("time",),
        )
    )
    rows.extend(
        _list_rows(
            "income",
            snapshot["income"],
            ("tranId", "incomeType", "symbol", "asset", "time"),
            time_fields=("time",),
        )
    )
    rows.extend(_list_rows("commission_rate", snapshot["commission_rates"], ("symbol",)))
    rows.extend(_list_rows("leverage_bracket", snapshot["leverage_brackets"], ("symbol",)))
    rows.extend(_list_rows("symbol_config", snapshot["symbol_config"], ("symbol",)))
    rows.extend(_list_rows("exchange_symbol", snapshot["exchange_symbols"], ("symbol",)))
    rows.extend(
        _list_rows(
            "order_rate_limit",
            snapshot["order_rate_limits"],
            ("rateLimitType", "interval", "intervalNum"),
        )
    )
    rows.extend(
        _row("watermark", str(name), value)
        for name, value in sorted(snapshot["watermarks"].items())
    )
    rows.extend(
        _row("check", str(value["name"]), value)
        for value in snapshot["validation"]["checks"]
    )
    keys = [(row["entity_type"], row["natural_key"]) for row in rows]
    if len(keys) != len(set(keys)):
        duplicates = sorted(key for key in set(keys) if keys.count(key) > 1)
        raise ValueError(f"Duplicate persistence natural keys: {duplicates[:20]}")
    rows.sort(key=lambda item: (item["entity_type"], item["natural_key"]))
    return rows


def _entity_hash(rows: list[dict[str, Any]]) -> str:
    material = [
        [row["entity_type"], row["natural_key"], row["payload_sha256"]] for row in rows
    ]
    return sha256_json(material)


def persist_snapshot(
    database_path: Path,
    snapshot: Mapping[str, Any],
    *,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    rows = snapshot_rows(snapshot)
    entity_hash = _entity_hash(rows)
    snapshot_gate_status = (
        "PASS" if not snapshot["validation"]["required_failures"] else "FAIL"
    )
    interval = snapshot["snapshot_interval"]
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR IGNORE INTO readonly_snapshot_runs (
                run_id, schema_version, toolkit_version, created_at_utc,
                account_fingerprint, status, snapshot_hash, entity_hash,
                entity_count, start_server_ms, end_server_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot["run_id"],
                snapshot["schema_version"],
                snapshot["toolkit_version"],
                snapshot["created_at_utc"],
                snapshot["account_fingerprint"],
                snapshot_gate_status,
                snapshot["snapshot_hash"],
                entity_hash,
                len(rows),
                interval["start_server_ms"],
                interval["end_server_ms"],
            ),
        )
        existing_run = connection.execute(
            "SELECT snapshot_hash, entity_hash, entity_count FROM readonly_snapshot_runs WHERE run_id=?",
            (snapshot["run_id"],),
        ).fetchone()
        if existing_run is None or tuple(existing_run) != (
            snapshot["snapshot_hash"],
            entity_hash,
            len(rows),
        ):
            raise ValueError("Immutable run_id already exists with different snapshot content")

        for index, row in enumerate(rows, start=1):
            connection.execute(
                """
                INSERT OR IGNORE INTO readonly_snapshot_entities (
                    run_id, entity_type, natural_key, symbol, position_side,
                    event_time_ms, payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["run_id"],
                    row["entity_type"],
                    row["natural_key"],
                    row["symbol"],
                    row["position_side"],
                    row["event_time_ms"],
                    row["payload_json"],
                    row["payload_sha256"],
                ),
            )
            persisted = connection.execute(
                """
                SELECT payload_sha256 FROM readonly_snapshot_entities
                WHERE run_id=? AND entity_type=? AND natural_key=?
                """,
                (snapshot["run_id"], row["entity_type"], row["natural_key"]),
            ).fetchone()
            if persisted is None or persisted[0] != row["payload_sha256"]:
                raise ValueError("Immutable entity key already exists with different content")
            if inject_failure_after is not None and index >= inject_failure_after:
                raise RuntimeError("R2.4 injected transaction failure")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "run_id": snapshot["run_id"],
        "entity_count": len(rows),
        "entity_hash": entity_hash,
        "status": "PASS",
        "snapshot_gate_status": snapshot_gate_status,
    }


def verify_database(database_path: Path, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    connection = _connect(database_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        run = connection.execute(
            """
            SELECT snapshot_hash, entity_hash, entity_count, account_fingerprint
            FROM readonly_snapshot_runs WHERE run_id=?
            """,
            (snapshot["run_id"],),
        ).fetchone()
        if run is None:
            raise ValueError("Persisted run was not found after reopening the database")
        entities = connection.execute(
            """
            SELECT entity_type, natural_key, payload_json, payload_sha256
            FROM readonly_snapshot_entities WHERE run_id=?
            ORDER BY entity_type, natural_key
            """,
            (snapshot["run_id"],),
        ).fetchall()
        for entity in entities:
            if sha256_bytes(entity["payload_json"].encode("utf-8")) != entity["payload_sha256"]:
                raise ValueError(
                    f"Entity checksum mismatch: {entity['entity_type']}:{entity['natural_key']}"
                )
        entity_hash = sha256_json(
            [
                [entity["entity_type"], entity["natural_key"], entity["payload_sha256"]]
                for entity in entities
            ]
        )
        if integrity != "ok" or foreign_keys:
            raise ValueError(f"SQLite integrity failure: integrity={integrity}, fk={len(foreign_keys)}")
        if run["snapshot_hash"] != snapshot["snapshot_hash"]:
            raise ValueError("Snapshot hash changed after database reopen")
        if run["entity_hash"] != entity_hash or run["entity_count"] != len(entities):
            raise ValueError("Entity hash/count changed after database reopen")
        return {
            "integrity_check": integrity,
            "foreign_key_violation_count": len(foreign_keys),
            "entity_count": len(entities),
            "entity_hash": entity_hash,
            "account_fingerprint": run["account_fingerprint"],
            "reopen_readback": "PASS",
        }
    finally:
        connection.close()


def verify_persistence(database_path: Path, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    first = persist_snapshot(database_path, snapshot)
    second = persist_snapshot(database_path, snapshot)
    if first != second:
        raise ValueError("Idempotent replay changed persistence result")

    rollback_probe = deepcopy(snapshot)
    rollback_probe["run_id"] = f"{snapshot['run_id']}-rollback-probe"
    rollback_probe["snapshot_hash"] = sha256_json(
        {key: value for key, value in rollback_probe.items() if key != "snapshot_hash"}
    )
    try:
        persist_snapshot(database_path, rollback_probe, inject_failure_after=1)
    except RuntimeError as exc:
        if "injected transaction failure" not in str(exc):
            raise
    else:
        raise ValueError("Rollback failure injection did not fail")

    connection = _connect(database_path)
    try:
        run_count = connection.execute(
            "SELECT COUNT(*) FROM readonly_snapshot_runs WHERE run_id=?",
            (rollback_probe["run_id"],),
        ).fetchone()[0]
        entity_count = connection.execute(
            "SELECT COUNT(*) FROM readonly_snapshot_entities WHERE run_id=?",
            (rollback_probe["run_id"],),
        ).fetchone()[0]
        if run_count or entity_count:
            raise ValueError("Injected transaction failure left partial rows")
    finally:
        connection.close()

    readback = verify_database(database_path, snapshot)
    return {
        **first,
        **readback,
        "idempotent_replay": "PASS",
        "rollback_probe": "PASS",
        "database_path": str(database_path),
    }
