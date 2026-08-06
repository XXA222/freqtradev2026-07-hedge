from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from freqtrade.hedge.planning.context import PlannerConfig
from freqtrade.hedge.simulation.exchange import SimulationInputEvent, SimulationResult
from freqtrade.hedge.simulation.replay import EventReplayEngine


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    name: str
    events: tuple[SimulationInputEvent, ...]
    initial_balance: Decimal = Decimal("1000")
    leverage: Decimal = Decimal("3")
    fee_rate: Decimal = Decimal("0.0004")
    planner_config: PlannerConfig = PlannerConfig()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("scenario name cannot be empty")
        if not self.events:
            raise ValueError("scenario requires at least one event")
        if not self.initial_balance.is_finite() or self.initial_balance <= 0:
            raise ValueError("initial balance must be finite and positive")
        if not self.leverage.is_finite() or self.leverage <= 0:
            raise ValueError("leverage must be finite and positive")
        if not self.fee_rate.is_finite() or self.fee_rate < 0 or self.fee_rate > 1:
            raise ValueError("fee rate must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    name: str
    result: SimulationResult
    invariants: tuple[str, ...]


class R4ScenarioRunner:
    @staticmethod
    def _engine(scenario: ScenarioDefinition) -> EventReplayEngine:
        return EventReplayEngine(
            initial_balance=scenario.initial_balance,
            leverage=scenario.leverage,
            fee_rate=scenario.fee_rate,
            planner_config=scenario.planner_config,
        )

    def run(self, scenario: ScenarioDefinition) -> ScenarioOutcome:
        result = self._engine(scenario).replay(scenario.events)
        return ScenarioOutcome(scenario.name, result, self.validate(result))

    def checkpoint_parity(self, scenario: ScenarioDefinition, split: int) -> bool:
        if split <= 0 or split >= len(scenario.events):
            raise ValueError("split must divide the scenario into two non-empty parts")
        batch_engine = self._engine(scenario)
        batch = batch_engine.replay(scenario.events)
        incremental = self._engine(scenario)
        incremental.advance(scenario.events[:split])
        checkpoint = incremental.checkpoint()
        restored = self._engine(scenario)
        restored.restore(checkpoint)
        resumed = restored.advance(scenario.events[split:])
        return (
            batch.report == resumed.report
            and batch_engine.checkpoint() == restored.checkpoint()
        )

    @staticmethod
    def validate(result: SimulationResult) -> tuple[str, ...]:
        report = result.report
        required = {
            "final_equity",
            "active_order_margin",
            "pnl_reconciliation_error",
            "final_long_quantity",
            "final_short_quantity",
        }
        missing = sorted(required - set(report))
        if missing:
            return tuple(f"MISSING_REPORT_FIELD:{name}" for name in missing)
        failures: list[str] = []
        numeric_values = {name: report[name] for name in required}
        for name, value in numeric_values.items():
            if not isinstance(value, Decimal) or not value.is_finite():
                failures.append(f"NON_FINITE_REPORT_VALUE:{name}")
        if failures:
            return tuple(failures)
        if report["final_equity"] < Decimal("0"):
            failures.append("NEGATIVE_FINAL_EQUITY")
        if report["active_order_margin"] < Decimal("0"):
            failures.append("NEGATIVE_ACTIVE_ORDER_MARGIN")
        if abs(report["pnl_reconciliation_error"]) > Decimal("1e-10"):
            failures.append("PNL_RECONCILIATION_DRIFT")
        if report["final_long_quantity"] < Decimal("0"):
            failures.append("NEGATIVE_LONG_QUANTITY")
        if report["final_short_quantity"] < Decimal("0"):
            failures.append("NEGATIVE_SHORT_QUANTITY")
        return tuple(failures)
