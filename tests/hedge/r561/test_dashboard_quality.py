from datetime import UTC, datetime
from decimal import Decimal

from freqtrade.hedge.r56.dashboard import OperationsDashboardSnapshot


def test_operations_summary_exposes_quality_not_just_running() -> None:
    snapshot = OperationsDashboardSnapshot(
        datetime(2026, 8, 6, tzinfo=UTC),
        "s",
        "c",
        ("BTCUSDT",),
        "RUNNING",
        True,
        True,
        None,
        None,
        None,
        (),
        (),
        True,
        last_candle_age_seconds=Decimal("10"),
        strategy_cycle_count=4,
        order_count=2,
        fill_count=1,
        runtime_quality_level=5,
        runtime_quality_state="LEDGER_UNVERIFIED",
    )
    summary = snapshot.summary()
    assert summary["last_candle_age_seconds"] == "10"
    assert summary["runtime_quality_level"] == 5
    assert summary["runtime_quality_state"] == "LEDGER_UNVERIFIED"
