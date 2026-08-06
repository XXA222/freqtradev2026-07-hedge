from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from .models import R5RunState, R5Stage


_ALLOWED: dict[R5RunState, frozenset[R5RunState]] = {
    R5RunState.NEW: frozenset({R5RunState.PREFLIGHT, R5RunState.BLOCKED, R5RunState.FAILED}),
    R5RunState.PREFLIGHT: frozenset({R5RunState.ARMED, R5RunState.EXECUTING, R5RunState.BLOCKED, R5RunState.FAILED}),
    R5RunState.ARMED: frozenset({R5RunState.EXECUTING, R5RunState.FAILED, R5RunState.RECOVERY_REQUIRED}),
    R5RunState.EXECUTING: frozenset({R5RunState.VERIFYING, R5RunState.RECOVERY_REQUIRED, R5RunState.FAILED}),
    R5RunState.VERIFYING: frozenset({R5RunState.COMPLETED, R5RunState.RECOVERY_REQUIRED, R5RunState.FAILED}),
    R5RunState.COMPLETED: frozenset(),
    R5RunState.BLOCKED: frozenset(),
    R5RunState.FAILED: frozenset(),
    R5RunState.RECOVERY_REQUIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class R5RunRecord:
    run_id: str
    stage: R5Stage
    state: R5RunState
    request_hash: str
    account_id: str
    actor: str
    config: Mapping[str, Any]
    report: Mapping[str, Any] | None
    error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class R5Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._initialize()

    def __enter__(self) -> "R5Journal":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.path.exists():
            with self._connect(allow_closed=True) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._closed = True

    def claim(
        self,
        *,
        run_id: str,
        stage: R5Stage,
        request_hash: str,
        account_id: str,
        actor: str,
        config: Mapping[str, Any],
    ) -> tuple[R5RunRecord, bool]:
        self._validate_identity(run_id, request_hash, account_id, actor)
        value = stage if isinstance(stage, R5Stage) else R5Stage(stage)
        now = datetime.now(UTC).isoformat()
        config_json = _json(config)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM r5_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is not None:
                record = self._record(row)
                if (record.stage, record.request_hash, record.account_id, record.actor) != (
                    value, request_hash, account_id, actor
                ):
                    raise RuntimeError("R5 run_id is already bound to different input")
                connection.commit()
                return record, True
            connection.execute(
                """
                INSERT INTO r5_runs(
                    run_id,stage,state,request_hash,account_id,actor,config_json,version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (run_id, value.value, R5RunState.NEW.value, request_hash, account_id, actor, config_json, 0, now, now),
            )
            self._event(connection, run_id, R5RunState.NEW, "CLAIMED", {"stage": value.value})
            connection.commit()
        return self.require(run_id), False

    def require(self, run_id: str) -> R5RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM r5_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._record(row)

    def transition(
        self,
        *,
        run_id: str,
        target: R5RunState,
        event: str,
        details: Mapping[str, Any] | None = None,
        report: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> R5RunRecord:
        target_state = target if isinstance(target, R5RunState) else R5RunState(target)
        event_name = str(event).strip().upper()
        if not event_name or len(event_name) > 128:
            raise ValueError("R5 event name is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM r5_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = self._record(row)
            if current.state is target_state:
                connection.commit()
                return current
            if target_state not in _ALLOWED[current.state]:
                raise RuntimeError(f"invalid R5 transition {current.state.value}->{target_state.value}")
            now = datetime.now(UTC).isoformat()
            result = connection.execute(
                """UPDATE r5_runs SET state=?,report_json=?,error=?,version=version+1,updated_at=?
                   WHERE run_id=? AND version=?""",
                (target_state.value, None if report is None else _json(report), error, now, run_id, current.version),
            )
            if result.rowcount != 1:
                raise RuntimeError("R5 journal compare-and-swap conflict")
            self._event(connection, run_id, target_state, event_name, details or {})
            connection.commit()
        return self.require(run_id)


    def append_event(
        self,
        *,
        run_id: str,
        event: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        event_name = str(event).strip().upper()
        if not event_name or len(event_name) > 128:
            raise ValueError("R5 event name is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM r5_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            self._event(
                connection,
                run_id,
                R5RunState(str(row[0])),
                event_name,
                details or {},
            )
            connection.commit()

    def assert_previous_complete(
        self,
        *,
        account_id: str,
        stage: R5Stage,
        require_promotion_ready: bool = False,
        max_age_seconds: int | None = None,
    ) -> None:
        previous = stage.previous
        if previous is None:
            return
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json,completed_at FROM r5_stage_evidence WHERE account_id=? AND stage=?",
                (account_id, previous.value),
            ).fetchone()
        if row is None:
            raise PermissionError(f"R5 stage {stage.value} requires completed {previous.value}")
        report = json.loads(str(row[0]))
        if require_promotion_ready:
            evidence = report.get("evidence") if isinstance(report, Mapping) else None
            if not isinstance(evidence, Mapping) or evidence.get("order_test_promotion_ready") is not True:
                raise PermissionError(
                    "R5 ORDER_TEST requires a fresh MAINNET_READONLY report with promotion readiness"
                )
        if max_age_seconds is not None:
            completed = datetime.fromisoformat(str(row[1]))
            if completed.tzinfo is None or completed.utcoffset() is None:
                completed = completed.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - completed.astimezone(UTC)).total_seconds()
            if age < -30 or age > max_age_seconds:
                raise PermissionError("R5 previous-stage evidence is stale or from the future")

    def complete_with_evidence(
        self,
        *,
        run_id: str,
        report: Mapping[str, Any],
        replace_stage_evidence: bool = False,
    ) -> tuple[R5RunRecord, str]:
        canonical = _json(report).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM r5_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = self._record(row)
            if current.state is R5RunState.COMPLETED:
                # MAINNET_READONLY is refreshable, so the current stage-evidence row may
                # legitimately point at a newer run. Replay identity must therefore be
                # checked against immutable history bound to this exact run_id.
                existing = connection.execute(
                    "SELECT report_sha256 FROM r5_stage_evidence_history WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if existing is None:
                    # Compatibility for databases created before evidence-history existed.
                    existing = connection.execute(
                        "SELECT report_sha256 FROM r5_stage_evidence WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                if existing is None or str(existing[0]) != digest:
                    raise RuntimeError("completed R5 run has inconsistent immutable evidence")
                connection.commit()
                return current, digest
            if current.state is not R5RunState.VERIFYING:
                raise RuntimeError("R5 completion requires VERIFYING state")
            existing = connection.execute(
                "SELECT report_sha256 FROM r5_stage_evidence WHERE account_id=? AND stage=?",
                (current.account_id, current.stage.value),
            ).fetchone()
            if (
                existing is not None
                and str(existing[0]) != digest
                and not replace_stage_evidence
            ):
                raise RuntimeError("R5 stage evidence already exists with different content")
            result = connection.execute(
                """UPDATE r5_runs SET state=?,report_json=?,error=NULL,version=version+1,updated_at=?
                   WHERE run_id=? AND version=?""",
                (R5RunState.COMPLETED.value, canonical.decode("utf-8"), now, run_id, current.version),
            )
            if result.rowcount != 1:
                raise RuntimeError("R5 journal compare-and-swap conflict")
            self._event(connection, run_id, R5RunState.COMPLETED, "RUN_COMPLETED", {})
            connection.execute(
                """INSERT OR IGNORE INTO r5_stage_evidence_history(
                    account_id,stage,run_id,report_sha256,report_json,completed_at
                ) VALUES(?,?,?,?,?,?)""",
                (current.account_id, current.stage.value, run_id, digest, canonical.decode("utf-8"), now),
            )
            if replace_stage_evidence:
                connection.execute(
                    """INSERT INTO r5_stage_evidence(
                        account_id,stage,run_id,report_sha256,report_json,completed_at
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account_id,stage) DO UPDATE SET
                        run_id=excluded.run_id,
                        report_sha256=excluded.report_sha256,
                        report_json=excluded.report_json,
                        completed_at=excluded.completed_at""",
                    (current.account_id, current.stage.value, run_id, digest, canonical.decode("utf-8"), now),
                )
            else:
                connection.execute(
                    """INSERT OR IGNORE INTO r5_stage_evidence(
                        account_id,stage,run_id,report_sha256,report_json,completed_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (current.account_id, current.stage.value, run_id, digest, canonical.decode("utf-8"), now),
                )
            connection.commit()
        return self.require(run_id), digest

    def record_stage_evidence(self, *, run_id: str, report: Mapping[str, Any]) -> str:
        record = self.require(run_id)
        if record.state is not R5RunState.COMPLETED:
            raise RuntimeError("only completed R5 runs can become promotion evidence")
        canonical = _json(report).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT report_sha256 FROM r5_stage_evidence WHERE account_id=? AND stage=?",
                (record.account_id, record.stage.value),
            ).fetchone()
            if existing is not None and str(existing[0]) != digest:
                raise RuntimeError("R5 stage evidence already exists with different content")
            connection.execute(
                """INSERT OR IGNORE INTO r5_stage_evidence(
                    account_id,stage,run_id,report_sha256,report_json,completed_at
                ) VALUES(?,?,?,?,?,?)""",
                (record.account_id, record.stage.value, run_id, digest, canonical.decode("utf-8"), now),
            )
            connection.commit()
        return digest

    def stage_evidence(self, *, account_id: str, stage: R5Stage) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM r5_stage_evidence WHERE account_id=? AND stage=?",
                (account_id, stage.value),
            ).fetchone()
        return None if row is None else json.loads(str(row[0]))

    def events(self, run_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state,event,details_json,created_at FROM r5_events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return tuple(
            {"state": row[0], "event": row[1], "details": json.loads(row[2]), "created_at": row[3]}
            for row in rows
        )

    @contextmanager
    def _connect(self, *, allow_closed: bool = False) -> Iterator[sqlite3.Connection]:
        if not allow_closed:
            self._ensure_open()
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS r5_runs(
                    run_id TEXT PRIMARY KEY, stage TEXT NOT NULL, state TEXT NOT NULL,
                    request_hash TEXT NOT NULL, account_id TEXT NOT NULL, actor TEXT NOT NULL,
                    config_json TEXT NOT NULL, report_json TEXT, error TEXT, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS r5_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, state TEXT NOT NULL,
                    event TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES r5_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS r5_stage_evidence(
                    account_id TEXT NOT NULL, stage TEXT NOT NULL, run_id TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL, report_json TEXT NOT NULL, completed_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,stage), UNIQUE(run_id),
                    FOREIGN KEY(run_id) REFERENCES r5_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS r5_stage_evidence_history(
                    account_id TEXT NOT NULL, stage TEXT NOT NULL, run_id TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL, report_json TEXT NOT NULL, completed_at TEXT NOT NULL,
                    PRIMARY KEY(run_id),
                    FOREIGN KEY(run_id) REFERENCES r5_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS ix_r5_stage_evidence_history_account_stage
                    ON r5_stage_evidence_history(account_id,stage,completed_at);
                """
            )

    def _event(self, connection: sqlite3.Connection, run_id: str, state: R5RunState, event: str, details: Mapping[str, Any]) -> None:
        connection.execute(
            "INSERT INTO r5_events(run_id,state,event,details_json,created_at) VALUES(?,?,?,?,?)",
            (run_id, state.value, event, _json(details), datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> R5RunRecord:
        return R5RunRecord(
            run_id=str(row["run_id"]), stage=R5Stage(row["stage"]), state=R5RunState(row["state"]),
            request_hash=str(row["request_hash"]), account_id=str(row["account_id"]), actor=str(row["actor"]),
            config=json.loads(row["config_json"]),
            report=None if row["report_json"] is None else json.loads(row["report_json"]),
            error=None if row["error"] is None else str(row["error"]), version=int(row["version"]),
            created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _validate_identity(run_id: str, request_hash: str, account_id: str, actor: str) -> None:
        for value, name, limit in ((run_id, "run_id", 128), (request_hash, "request_hash", 128), (account_id, "account_id", 128), (actor, "actor", 128)):
            text = str(value).strip()
            if not text or len(text) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
                raise ValueError(f"{name} is invalid")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("R5 journal is closed")


def _json(value: Mapping[str, Any] | object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
