from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from freqtrade.hedge.readonly.soak_monitor import SoakMonitor

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


def build_soak_evidence(
    *,
    observations_path: str | Path,
    account_id: str,
    producer: str = "r54-soak-evaluator",
    expected_interval_seconds: float = 60.0,
    drift_tolerance: Decimal = Decimal("0.00000001"),
) -> tuple[EvidenceEnvelope, EvidenceEnvelope | None]:
    monitor = SoakMonitor(
        observations_path,
        expected_interval_seconds=expected_interval_seconds,
        drift_tolerance=drift_tolerance,
    )
    summary = monitor.summarize()
    payload: dict[str, Any] = asdict(summary)
    payload["max_abs_unexplained_drift"] = str(summary.max_abs_unexplained_drift)
    payload["observations_path"] = Path(observations_path).name
    created_at = datetime.now(UTC)
    evidence_24 = create_envelope(
        kind=ArtifactKind.MAINNET_READONLY_24H,
        account_id=account_id,
        environment="MAINNET",
        producer=producer,
        payload=payload,
        status=CheckStatus.PASS if summary.passed_24h else CheckStatus.FAIL,
        ttl_seconds=7 * 24 * 3600,
        created_at=created_at,
        write_surface="NONE",
        real_order_requests=0,
    )
    evidence_72 = None
    if summary.meets_72h:
        evidence_72 = create_envelope(
            kind=ArtifactKind.MAINNET_READONLY_72H,
            account_id=account_id,
            environment="MAINNET",
            producer=producer,
            payload=payload,
            status=CheckStatus.PASS if summary.passed_72h else CheckStatus.FAIL,
            ttl_seconds=7 * 24 * 3600,
            created_at=created_at,
            write_surface="NONE",
            real_order_requests=0,
        )
    return evidence_24, evidence_72
