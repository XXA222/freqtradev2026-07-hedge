from datetime import UTC, datetime, timedelta
from decimal import Decimal

from freqtrade.hedge.r561.network import ProbeAttempt, evaluate_probe_attempts


def test_network_probe_requires_all_attempts_and_fresh_monotonic_candles() -> None:
    now = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
    attempts = tuple(
        ProbeAttempt(
            sequence=i + 1,
            ok=True,
            latency_ms=10,
            observed_at=now,
            candle_timestamp=now - timedelta(seconds=60 - i),
        )
        for i in range(20)
    )
    report = evaluate_probe_attempts(
        attempts,
        required_attempts=20,
        minimum_success_ratio=Decimal("1"),
        now=now,
    )
    assert report.ready and report.succeeded == 20 and report.failed == 0


def test_network_probe_rejects_failure_stale_and_gap() -> None:
    now = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
    attempts = (
        ProbeAttempt(1, True, 10, now, now - timedelta(minutes=5)),
        ProbeAttempt(2, False, 20, now, error="NetworkError"),
    )
    report = evaluate_probe_attempts(
        attempts,
        required_attempts=2,
        minimum_success_ratio=Decimal("1"),
        max_last_candle_age_seconds=90,
        now=now,
    )
    assert not report.ready
    assert any(reason.startswith("NETWORK_SUCCESS_RATIO_LOW") for reason in report.reasons)
    assert any(reason.startswith("NETWORK_LAST_CANDLE_STALE") for reason in report.reasons)
