import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from freqtrade.hedge.r4.models import R4CycleReport, R4Mode, RuntimePhase
from freqtrade.hedge.r4.observability import MetricsRegistry
from freqtrade.hedge.r4.parity import PlannerParityVerifier
from freqtrade.hedge.r4.planner import R4HedgePlanner, R4PlanningInputs
from freqtrade.hedge.r4.status import RuntimeStatusPublisher
from freqtrade.hedge.r4.testnet_matrix import R4TestnetMatrixRunner
from freqtrade.hedge.execution.testnet_e2e import TestnetScenarioState
from tools.hedge_r40.tests.test_r40_planner_runtime import context


class MatrixOrchestrator:
    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if "btcusdt-short" in kwargs["run_id"]:
            raise RuntimeError("synthetic failure")
        return SimpleNamespace(
            final_state=TestnetScenarioState.COMPLETED,
            status="TESTNET_E2E_COMPLETE",
        )


def test_testnet_matrix_continues_all_cases_after_failure():
    orchestrator = MatrixOrchestrator()
    report = asyncio.run(
        R4TestnetMatrixRunner(orchestrator).run(
            credentials=object(),
            arm_token="x" * 16,
            actor="test",
            execution_transport=object(),
            include_submit_cancel=True,
        )
    )
    assert len(report.results) == 6
    assert len(orchestrator.calls) == 6
    assert len(report.failures) == 1


def test_planner_fingerprint_is_identical_across_all_surfaces():
    result = PlannerParityVerifier(R4HedgePlanner()).verify(
        R4PlanningInputs(context(), realized_volatility=Decimal("0.01")),
        ("BACKTEST", "DRY_RUN", "TESTNET", "PRODUCTION_LOCKED"),
    )
    assert len(set(result.values())) == 1


def test_status_publisher_writes_atomic_json_and_prometheus(tmp_path):
    from freqtrade.hedge.r4.models import RuntimeReadiness
    metrics = MetricsRegistry()
    metrics.increment("cycle.total", 2)
    readiness = RuntimeReadiness(RuntimePhase.READY, True, False, (), ())
    cycle = R4CycleReport("c1", True, "LOCKED", writes_attempted=0)
    publisher = RuntimeStatusPublisher(tmp_path / "status.json", metrics)
    publisher.publish(readiness=readiness, last_cycle=cycle)
    payload = json.loads((tmp_path / "status.json").read_text())
    assert payload["readiness"]["mainnet_live_exchange_write"] == "LOCKED"
    assert "freqtrade_hedge_cycle_total 2" in publisher.prometheus_text()
