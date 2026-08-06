from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from freqtrade.hedge.r5.models import R5Stage

from .canonical import parse_aware_datetime
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope, GateDecision


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS r54_evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    account_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    canonical_sha256 TEXT NOT NULL UNIQUE,
    envelope_json TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_r54_evidence_account_kind_created
    ON r54_evidence(account_id, kind, created_at DESC);
CREATE TABLE IF NOT EXISTS r54_gate_decisions (
    decision_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    canonical_sha256 TEXT NOT NULL UNIQUE,
    decision_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_r54_gate_account_stage_time
    ON r54_gate_decisions(account_id, stage, evaluated_at DESC);
CREATE TABLE IF NOT EXISTS r54_incidents (
    incident_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    payload_json TEXT NOT NULL
);
"""


class R54OperationalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def put_evidence(self, envelope: EvidenceEnvelope) -> str:
        evidence_id = uuid.uuid4().hex
        payload = envelope.to_dict()
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO r54_evidence(
                    evidence_id, kind, account_id, environment, status, created_at,
                    expires_at, canonical_sha256, envelope_json, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence_id,
                    envelope.kind.value,
                    envelope.account_id,
                    envelope.environment,
                    envelope.status.value,
                    envelope.created_at.astimezone(UTC).isoformat(),
                    None
                    if envelope.expires_at is None
                    else envelope.expires_at.astimezone(UTC).isoformat(),
                    envelope.canonical_sha256,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT evidence_id FROM r54_evidence WHERE canonical_sha256 = ?",
                (envelope.canonical_sha256,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("evidence insert did not produce a row")
        return str(row["evidence_id"])

    def list_evidence(
        self,
        *,
        account_id: str,
        kinds: Iterable[ArtifactKind] | None = None,
    ) -> tuple[EvidenceEnvelope, ...]:
        values = tuple(kinds or ())
        query = "SELECT envelope_json FROM r54_evidence WHERE account_id = ?"
        params: list[object] = [account_id]
        if values:
            placeholders = ",".join("?" for _ in values)
            query += f" AND kind IN ({placeholders})"
            params.extend(item.value for item in values)
        query += " ORDER BY created_at DESC"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_envelope_from_dict(json.loads(str(row["envelope_json"]))) for row in rows)

    def latest_evidence(
        self,
        *,
        account_id: str,
        kind: ArtifactKind,
    ) -> EvidenceEnvelope | None:
        values = self.list_evidence(account_id=account_id, kinds=(kind,))
        return values[0] if values else None

    def put_decision(self, decision: GateDecision) -> str:
        decision_id = uuid.uuid4().hex
        payload = decision.to_dict()
        canonical = str(payload["canonical_sha256"])
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO r54_gate_decisions(
                    decision_id, account_id, stage, status, evaluated_at,
                    canonical_sha256, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    decision.account_id,
                    decision.stage.value,
                    decision.status.value,
                    decision.evaluated_at.astimezone(UTC).isoformat(),
                    canonical,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
            )
            row = connection.execute(
                "SELECT decision_id FROM r54_gate_decisions WHERE canonical_sha256 = ?",
                (canonical,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("decision insert did not produce a row")
        return str(row["decision_id"])

    def integrity_check(self) -> tuple[bool, str]:
        with closing(self._connect()) as connection:
            value = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return value.lower() == "ok", value


def _envelope_from_dict(value: dict[str, object]) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        kind=ArtifactKind(str(value["kind"])),
        account_id=str(value["account_id"]),
        environment=str(value["environment"]),
        status=CheckStatus(str(value["status"])),
        created_at=parse_aware_datetime(value["created_at"], "created_at"),
        expires_at=(
            None
            if value.get("expires_at") is None
            else parse_aware_datetime(value["expires_at"], "expires_at")
        ),
        producer=str(value["producer"]),
        payload=dict(value["payload"]),  # type: ignore[arg-type]
        payload_sha256=str(value["payload_sha256"]),
        write_surface=str(value.get("write_surface", "NONE")),
        real_order_requests=int(value.get("real_order_requests", 0)),
        schema_version=int(value.get("schema_version", 1)),
    )
