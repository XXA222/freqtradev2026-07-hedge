from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from freqtrade.hedge.r54.evidence import create_envelope, load_envelope, save_envelope
from freqtrade.hedge.r54.models import ArtifactKind, CheckStatus
from freqtrade.hedge.r54.store import R54OperationalStore


def test_readonly_envelope_rejects_writes() -> None:
    with pytest.raises(ValueError, match="read-only"):
        create_envelope(
            kind=ArtifactKind.MAINNET_READONLY_24H,
            account_id="acct",
            environment="MAINNET",
            producer="test",
            payload={"passed": True},
            write_surface="POST",
            real_order_requests=1,
        )


def test_envelope_roundtrip_and_store_idempotency(tmp_path) -> None:
    envelope = create_envelope(
        kind=ArtifactKind.MAINNET_READONLY_PREFLIGHT,
        account_id="acct",
        environment="MAINNET",
        producer="test",
        payload={"clock_offset_ms": 5},
        ttl_seconds=300,
    )
    path = tmp_path / "evidence.json"
    save_envelope(path, envelope)
    loaded = load_envelope(path)
    assert loaded == envelope
    store = R54OperationalStore(tmp_path / "authority.sqlite")
    first = store.put_evidence(loaded)
    second = store.put_evidence(loaded)
    assert first == second
    assert store.latest_evidence(
        account_id="acct", kind=ArtifactKind.MAINNET_READONLY_PREFLIGHT
    ) == envelope
    assert store.integrity_check() == (True, "ok")


def test_evidence_freshness() -> None:
    created = datetime(2026, 8, 5, tzinfo=UTC)
    envelope = create_envelope(
        kind=ArtifactKind.MAINNET_READONLY_PREFLIGHT,
        account_id="acct",
        environment="MAINNET",
        producer="test",
        payload={"ok": True},
        ttl_seconds=60,
        created_at=created,
    )
    assert envelope.is_fresh(created + timedelta(seconds=59))
    assert not envelope.is_fresh(created + timedelta(seconds=60))
