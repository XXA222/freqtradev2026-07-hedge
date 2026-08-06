from datetime import UTC, datetime
from decimal import Decimal

from freqtrade.hedge.r561.network import NetworkProbeReport
from freqtrade.hedge.r561.quality import (
    RuntimeCredibilityEvidence,
    RuntimeQualityState,
    evaluate_runtime_credibility,
)


def _network() -> NetworkProbeReport:
    return NetworkProbeReport(True, 20, 20, 0, Decimal("1"), 10, 20, 0, (), ())


def test_running_is_not_credible_without_strategy_orders_ledger_and_ui() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    result = evaluate_runtime_credibility(
        RuntimeCredibilityEvidence(now, True, True, _network(), _network())
    )
    assert not result.credible
    assert result.highest_passed_layer == 3
    assert result.state is RuntimeQualityState.STRATEGY_STALE


def test_all_seven_layers_are_required_for_credible_simulation() -> None:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    result = evaluate_runtime_credibility(
        RuntimeCredibilityEvidence(
            observed_at=now,
            process_running=True,
            exchange_initialized=True,
            ticker_probe=_network(),
            ohlcv_probe=_network(),
            last_strategy_cycle_at=now,
            strategy_cycle_count=10,
            order_count=4,
            fill_count=3,
            deterministic_scenario_passed=True,
            ledger_consistent=True,
            restart_recovered=True,
            reconciliation_at=now,
            api_healthy=True,
            dashboard_healthy=True,
        )
    )
    assert result.credible and result.highest_passed_layer == 7
    assert result.state is RuntimeQualityState.CREDIBLE
