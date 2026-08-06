from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .canonical import atomic_write_json, parse_aware_datetime, sha256_json
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope, READ_ONLY_KINDS


def create_envelope(
    *,
    kind: ArtifactKind,
    account_id: str,
    environment: str,
    producer: str,
    payload: Mapping[str, Any],
    status: CheckStatus = CheckStatus.PASS,
    ttl_seconds: int | None = None,
    created_at: datetime | None = None,
    write_surface: str = "NONE",
    real_order_requests: int = 0,
) -> EvidenceEnvelope:
    if ttl_seconds is not None and (
        isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0
    ):
        raise ValueError("ttl_seconds must be a positive integer")
    created = (created_at or datetime.now(UTC)).astimezone(UTC)
    expires = None if ttl_seconds is None else created + timedelta(seconds=ttl_seconds)
    return EvidenceEnvelope(
        kind=kind,
        account_id=account_id,
        environment=environment.upper(),
        status=status,
        created_at=created,
        expires_at=expires,
        producer=producer,
        payload=dict(payload),
        payload_sha256=sha256_json(dict(payload)),
        write_surface=write_surface.upper(),
        real_order_requests=real_order_requests,
    )


def save_envelope(path: str | Path, envelope: EvidenceEnvelope) -> None:
    atomic_write_json(path, envelope.to_dict())


def load_envelope(path: str | Path) -> EvidenceEnvelope:
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, Mapping):
        raise ValueError("evidence file must contain a JSON object")
    envelope = EvidenceEnvelope(
        kind=ArtifactKind(str(raw["kind"])),
        account_id=str(raw["account_id"]),
        environment=str(raw["environment"]),
        status=CheckStatus(str(raw["status"])),
        created_at=parse_aware_datetime(raw["created_at"], "created_at"),
        expires_at=(
            None
            if raw.get("expires_at") is None
            else parse_aware_datetime(raw["expires_at"], "expires_at")
        ),
        producer=str(raw["producer"]),
        payload=dict(raw["payload"]),  # type: ignore[arg-type]
        payload_sha256=str(raw["payload_sha256"]),
        write_surface=str(raw.get("write_surface", "NONE")),
        real_order_requests=int(raw.get("real_order_requests", 0)),
        schema_version=int(raw.get("schema_version", 1)),
    )
    expected = str(raw.get("canonical_sha256", ""))
    if expected and expected != envelope.canonical_sha256:
        raise ValueError("evidence canonical_sha256 mismatch")
    return envelope


def verify_envelope(
    envelope: EvidenceEnvelope,
    *,
    expected_account_id: str,
    expected_environment: str | None = None,
    now: datetime | None = None,
) -> None:
    if envelope.account_id != expected_account_id:
        raise PermissionError("evidence account binding mismatch")
    if expected_environment is not None and envelope.environment != expected_environment.upper():
        raise PermissionError("evidence environment mismatch")
    if envelope.status is not CheckStatus.PASS:
        raise PermissionError("evidence status is not PASS")
    if not envelope.is_fresh(now):
        raise PermissionError("evidence is stale")
    if envelope.kind in READ_ONLY_KINDS:
        if envelope.write_surface != "NONE" or envelope.real_order_requests != 0:
            raise PermissionError("read-only evidence reports exchange writes")
