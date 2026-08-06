from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class OperationalMetrics:
    stream_age_seconds: float
    clock_offset_ms: int
    unknown_order_count: int
    reconciliation_diff_count: int
    outbox_backlog: int
    database_bytes: int
    process_rss_bytes: int
    api_weight_ratio: Decimal


@dataclass(frozen=True, slots=True)
class AlertThresholds:
    max_stream_age_seconds: float = 15.0
    max_abs_clock_offset_ms: int = 1000
    max_unknown_orders: int = 0
    max_reconciliation_diffs: int = 0
    max_outbox_backlog: int = 0
    max_api_weight_ratio: Decimal = Decimal("0.80")


def evaluate_alerts(metrics: OperationalMetrics, limits: AlertThresholds) -> Mapping[str, bool]:
    return {
        "stream_fresh": metrics.stream_age_seconds <= limits.max_stream_age_seconds,
        "clock_safe": abs(metrics.clock_offset_ms) <= limits.max_abs_clock_offset_ms,
        "unknown_clear": metrics.unknown_order_count <= limits.max_unknown_orders,
        "reconciliation_clean": (
            metrics.reconciliation_diff_count <= limits.max_reconciliation_diffs
        ),
        "outbox_clear": metrics.outbox_backlog <= limits.max_outbox_backlog,
        "api_weight_safe": metrics.api_weight_ratio <= limits.max_api_weight_ratio,
    }


def build_alert_drill_evidence(
    *,
    account_id: str,
    normal_metrics: OperationalMetrics,
    injected_metrics: OperationalMetrics,
    thresholds: AlertThresholds = AlertThresholds(),
) -> EvidenceEnvelope:
    normal = evaluate_alerts(normal_metrics, thresholds)
    injected = evaluate_alerts(injected_metrics, thresholds)
    injected_detected = any(not value for value in injected.values())
    passed = all(normal.values()) and injected_detected
    return create_envelope(
        kind=ArtifactKind.ALERT_DRILL,
        account_id=account_id,
        environment="LOCAL",
        producer="r54-alert-drill",
        payload={
            "normal": dict(normal),
            "injected": dict(injected),
            "injected_detected": injected_detected,
        },
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        ttl_seconds=7 * 24 * 3600,
        write_surface="NONE",
    )
