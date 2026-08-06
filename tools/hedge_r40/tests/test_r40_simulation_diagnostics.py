from datetime import UTC, datetime, timedelta
from decimal import Decimal

from freqtrade.hedge.planning.context import PlannerConfig
from freqtrade.hedge.r4.diagnostics import DiagnosticRunner
from freqtrade.hedge.r4.simulation import R4ScenarioRunner, ScenarioDefinition
from freqtrade.hedge.simulation.exchange import BarEvent, SignalEvent


def scenario():
    start = datetime(2026, 8, 3, tzinfo=UTC)
    events = [SignalEvent(start, "ETHUSDT", Decimal("1"), Decimal("1"), model_version="r4")]
    for i, value in enumerate(("2000", "1980", "2020", "1975", "2030", "1995"), start=1):
        price = Decimal(value)
        events.append(
            BarEvent(
                start + timedelta(minutes=i), "ETHUSDT", price,
                price * Decimal("1.02"), price * Decimal("0.98"), price, Decimal("100"),
            )
        )
    return ScenarioDefinition(
        "parity", tuple(events), planner_config=PlannerConfig(
            core_wallet_exposure_long=Decimal("0.01"),
            core_wallet_exposure_short=Decimal("0.01"),
            tactical_wallet_exposure_long=Decimal("0.005"),
            tactical_wallet_exposure_short=Decimal("0.005"),
            max_single_order_notional=Decimal("25"), cooldown_seconds=0,
        )
    )


def test_simulation_invariants_and_checkpoint_parity():
    runner = R4ScenarioRunner()
    definition = scenario()
    outcome = runner.run(definition)
    assert outcome.invariants == ()
    assert runner.checkpoint_parity(definition, 3)


def test_diagnostic_runner_continues_after_failures(tmp_path):
    runner = DiagnosticRunner(tmp_path)
    runner.run_callable("first", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    runner.run_callable("second", lambda: "ok")
    from datetime import datetime, UTC
    summary = runner.finish(datetime.now(UTC))
    assert len(summary.results) == 2
    assert summary.results[0].status == "FAIL"
    assert summary.results[1].status == "PASS"
    assert (tmp_path / "R4-DIAGNOSTIC-SUMMARY.json").is_file()
