from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from freqtrade.hedge.r54.incident import IncidentDrill, IncidentState
from freqtrade.hedge.r54.models import CheckStatus
from freqtrade.hedge.r54.observability import (
    AlertThresholds,
    OperationalMetrics,
    build_alert_drill_evidence,
)
from freqtrade.hedge.r54.resources import ResourceSample, build_resource_soak_evidence


def _metrics(*, stream_age: float) -> OperationalMetrics:
    return OperationalMetrics(
        stream_age_seconds=stream_age,
        clock_offset_ms=5,
        unknown_order_count=0,
        reconciliation_diff_count=0,
        outbox_backlog=0,
        database_bytes=100,
        process_rss_bytes=1000,
        api_weight_ratio=Decimal("0.10"),
    )


def test_alert_drill_requires_normal_clean_and_injected_detected() -> None:
    evidence = build_alert_drill_evidence(
        account_id="acct",
        normal_metrics=_metrics(stream_age=1),
        injected_metrics=_metrics(stream_age=999),
        thresholds=AlertThresholds(max_stream_age_seconds=15),
    )
    assert evidence.status is CheckStatus.PASS


def test_resource_soak_detects_growth() -> None:
    start = datetime(2026, 8, 5, tzinfo=UTC)
    samples = (
        ResourceSample(start, 100, 10, 5, 1000),
        ResourceSample(start + timedelta(hours=1), 110, 11, 6, 1200),
    )
    evidence = build_resource_soak_evidence(samples, account_id="acct")
    assert evidence.status is CheckStatus.PASS


def test_incident_drill_state_machine() -> None:
    drill = IncidentDrill.open(incident_id="i1", account_id="acct", reason="test")
    with pytest.raises(ValueError):
        drill.transition(IncidentState.CLOSED, actor="op", note="skip")
    drill.transition(IncidentState.CONTAINED, actor="op", note="kill switch")
    drill.transition(IncidentState.RECOVERED, actor="op", note="reconciled")
    drill.transition(IncidentState.CLOSED, actor="risk", note="reviewed")
    assert drill.evidence().status is CheckStatus.PASS
