from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class ResourceSample:
    observed_at: datetime
    rss_bytes: int
    open_file_count: int
    thread_count: int
    database_bytes: int

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in ("rss_bytes", "open_file_count", "thread_count", "database_bytes"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")


def build_resource_soak_evidence(
    samples: Iterable[ResourceSample],
    *,
    account_id: str,
    max_rss_growth_ratio: float = 1.25,
    max_open_file_growth: int = 8,
    max_thread_growth: int = 4,
) -> EvidenceEnvelope:
    ordered = tuple(sorted(samples, key=lambda item: item.observed_at))
    if len(ordered) < 2:
        raise ValueError("at least two resource samples are required")
    first, last = ordered[0], ordered[-1]
    rss_ratio = 1.0 if first.rss_bytes == 0 else last.rss_bytes / first.rss_bytes
    open_growth = last.open_file_count - first.open_file_count
    thread_growth = last.thread_count - first.thread_count
    duration = (last.observed_at - first.observed_at).total_seconds()
    passed = (
        duration > 0
        and rss_ratio <= max_rss_growth_ratio
        and open_growth <= max_open_file_growth
        and thread_growth <= max_thread_growth
    )
    return create_envelope(
        kind=ArtifactKind.RESOURCE_SOAK,
        account_id=account_id,
        environment="LOCAL",
        producer="r54-resource-soak",
        payload={
            "sample_count": len(ordered),
            "duration_seconds": duration,
            "rss_growth_ratio": rss_ratio,
            "open_file_growth": open_growth,
            "thread_growth": thread_growth,
            "database_growth_bytes": last.database_bytes - first.database_bytes,
            "limits": {
                "max_rss_growth_ratio": max_rss_growth_ratio,
                "max_open_file_growth": max_open_file_growth,
                "max_thread_growth": max_thread_growth,
            },
        },
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        ttl_seconds=7 * 24 * 3600,
        created_at=datetime.now(UTC),
        write_surface="NONE",
    )
