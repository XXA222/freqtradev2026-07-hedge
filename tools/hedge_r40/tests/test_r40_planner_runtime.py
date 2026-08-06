from datetime import UTC, datetime
from decimal import Decimal

import pytest

from freqtrade.hedge.planning.context import (
    LegPosition, MarketSnapshot, PlannerConfig, PlanningContext, PositionSide, WalletSnapshot,
)
from freqtrade.hedge.r4.config import runtime_config_from_mapping
from freqtrade.hedge.r4.lifecycle import R4RuntimeLifecycle
from freqtrade.hedge.r4.planner import R4HedgePlanner, R4PlanningInputs
from freqtrade.hedge.r4.runtime import R4ProductionRuntime, StaticComponent


NAMES = (
    "configuration", "database", "readonly_rest", "user_stream", "reconciliation",
    "planner", "risk", "execution", "control_plane", "supervisor",
)


def context() -> PlanningContext:
    return PlanningContext(
        market=MarketSnapshot(
            "ETHUSDT", datetime.now(UTC), Decimal("1999.9"),
            Decimal("2000.1"), Decimal("2000"), Decimal("0.01"), Decimal("0.001"),
            Decimal("0.001"), Decimal("5"),
        ),
        wallet=WalletSnapshot(
            Decimal("1000"), Decimal("1000"), Decimal("1000"),
            LegPosition(PositionSide.LONG), LegPosition(PositionSide.SHORT), leverage=Decimal("3"),
        ),
        config=PlannerConfig(
            core_wallet_exposure_long=Decimal("0.01"),
            core_wallet_exposure_short=Decimal("0.01"),
            tactical_wallet_exposure_long=Decimal("0.005"),
            tactical_wallet_exposure_short=Decimal("0.005"),
            max_single_order_notional=Decimal("25"),
        ),
        long_signal=Decimal("1"), short_signal=Decimal("1"),
    )


def test_r4_planner_expands_grid_for_volatility():
    decision = R4HedgePlanner().plan(
        R4PlanningInputs(context(), realized_volatility=Decimal("0.02"))
    )
    assert decision.effective_config.grid_spacing == Decimal("0.030")


def test_positive_funding_suppresses_long_entries():
    decision = R4HedgePlanner().plan(
        R4PlanningInputs(context(), funding_rate=Decimal("0.002"))
    )
    assert "LONG_ENTRY_SUPPRESSED_BY_POSITIVE_FUNDING" in decision.risk_flags
    assert all(order.position_side is not PositionSide.LONG for order in decision.result.submit_orders)


def test_locked_runtime_plans_but_never_writes(tmp_path):
    config = runtime_config_from_mapping(
        {"hedge_r4": {"mode": "PRODUCTION_LOCKED", "account_id": "binance-usdm:x"}},
        project_root=tmp_path,
    )
    lifecycle = R4RuntimeLifecycle(tuple(StaticComponent(name) for name in NAMES))
    runtime = R4ProductionRuntime(config=config, lifecycle=lifecycle, context_provider=context)
    assert runtime.start().ready
    cycle = runtime.run_once()
    assert cycle.processed
    assert cycle.reason == "PRODUCTION_LOCKED_PLAN_ONLY"
    assert cycle.writes_attempted == 0
    assert cycle.writes_blocked == cycle.submissions_planned + cycle.cancellations_planned


def test_runtime_blocks_cycles_when_component_degraded(tmp_path):
    class DegradedComponent(StaticComponent):
        def start(self):
            self.state = self.state.DEGRADED

    components = [StaticComponent(name) for name in NAMES]
    components[3] = DegradedComponent(NAMES[3])
    config = runtime_config_from_mapping(
        {"hedge_r4": {"mode": "PRODUCTION_LOCKED", "account_id": "binance-usdm:x"}},
        project_root=tmp_path,
    )
    runtime = R4ProductionRuntime(
        config=config,
        lifecycle=R4RuntimeLifecycle(tuple(components)),
        context_provider=context,
    )
    assert not runtime.start().ready
    report = runtime.run_once()
    assert not report.processed
    assert report.reason == "READINESS_BLOCKED"
