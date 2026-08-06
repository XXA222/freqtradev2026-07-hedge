import asyncio
import json
from pathlib import Path
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from freqtrade.hedge.execution.testnet_e2e import TestnetScenarioState
from freqtrade.hedge.r4.config import runtime_config_from_mapping
from freqtrade.hedge.r4.lifecycle import R4RuntimeLifecycle
from freqtrade.hedge.r4.models import ComponentHealth, ComponentState, R4Mode, RuntimePhase
from freqtrade.hedge.r4.observability import JsonlEventSink, MetricsRegistry, redact
from freqtrade.hedge.r4.parity import PlannerParityVerifier
from freqtrade.hedge.r4.planner import R4HedgePlanner, R4PlanningInputs
from freqtrade.hedge.r4.readiness import ReadinessAggregator
from freqtrade.hedge.r4.runtime import R4ExecutionOutcome, R4ProductionRuntime, StaticComponent
from freqtrade.hedge.r4.testnet_matrix import R4TestnetMatrixRunner
from tools.hedge_r40.r40_toolkit.forty_round_audit import run_forty_round_audit
from tools.hedge_r40.tests.test_r40_planner_runtime import NAMES, context


def test_strict_boolean_parser_handles_string_false(tmp_path):
    config = runtime_config_from_mapping(
        {"planner_enabled": "false", "control_plane_enabled": "true"},
        project_root=tmp_path,
    )
    assert config.planner_enabled is False
    assert config.control_plane_enabled is True


@pytest.mark.parametrize("value", ["maybe", 2, [], None])
def test_strict_boolean_parser_rejects_ambiguous_values(tmp_path, value):
    with pytest.raises(ValueError):
        runtime_config_from_mapping({"planner_enabled": value}, project_root=tmp_path)


def test_required_component_cannot_mark_itself_optional():
    aggregator = ReadinessAggregator(mode=R4Mode.PRODUCTION_LOCKED)
    aggregator.update(ComponentHealth("database", ComponentState.FAILED, required=False))
    assert any(item.startswith("database:") for item in aggregator.snapshot(RuntimePhase.READY).blockers)


def test_stale_stream_health_blocks_readiness():
    aggregator = ReadinessAggregator(
        mode=R4Mode.TESTNET,
        max_age_seconds={"user_stream": 5},
    )
    for name in NAMES:
        timestamp = datetime.now(UTC) - timedelta(seconds=10) if name == "user_stream" else datetime.now(UTC)
        aggregator.update(ComponentHealth(name, ComponentState.READY, updated_at=timestamp))
    assert not aggregator.snapshot(RuntimePhase.READY).ready


def test_dry_run_without_execution_handler_never_claims_writes(tmp_path):
    config = runtime_config_from_mapping(
        {"mode": "DRY_RUN", "account_id": "binance-usdm:audit"}, project_root=tmp_path
    )
    runtime = R4ProductionRuntime(
        config=config,
        lifecycle=R4RuntimeLifecycle(tuple(StaticComponent(name) for name in NAMES)),
        context_provider=context,
    )
    runtime.start()
    result = runtime.run_once()
    assert result.reason == "EXECUTION_HANDLER_UNAVAILABLE"
    assert result.writes_attempted == 0
    assert not result.processed


def test_dry_run_execution_handler_reports_real_attempt_count(tmp_path):
    config = runtime_config_from_mapping(
        {"mode": "DRY_RUN", "account_id": "binance-usdm:audit"}, project_root=tmp_path
    )
    runtime = R4ProductionRuntime(
        config=config,
        lifecycle=R4RuntimeLifecycle(tuple(StaticComponent(name) for name in NAMES)),
        context_provider=context,
        execution_handler=lambda result: R4ExecutionOutcome(
            "SIMULATED", len(result.submit_orders) + len(result.cancel_order_ids)
        ),
    )
    runtime.start()
    result = runtime.run_once(cycle_key="bar-1")
    assert result.processed
    assert result.writes_attempted == result.submissions_planned + result.cancellations_planned
    duplicate = runtime.run_once(cycle_key="bar-1")
    assert duplicate.reason == "DUPLICATE_CYCLE"


def test_runtime_config_caps_override_looser_strategy_caps(tmp_path):
    config = runtime_config_from_mapping(
        {
            "mode": "PRODUCTION_LOCKED",
            "account_id": "binance-usdm:audit",
            "max_single_order_notional": "10",
        },
        project_root=tmp_path,
    )
    runtime = R4ProductionRuntime(
        config=config,
        lifecycle=R4RuntimeLifecycle(tuple(StaticComponent(name) for name in NAMES)),
        context_provider=context,
    )
    runtime.start()
    result = runtime.run_once()
    assert max(order.notional for order in result.planning_result.ideal_orders) <= Decimal("10")


def test_redaction_covers_signed_url_and_bearer_value():
    value = redact(
        {
            "url": "https://example.test/path?signature=abc&token=xyz",
            "header": "Bearer private-token",
        }
    )
    assert "abc" not in value["url"]
    assert "xyz" not in value["url"]
    assert "private-token" not in value["header"]


def test_event_sink_serializes_path_bytes_and_enum(tmp_path):
    sink = JsonlEventSink(tmp_path / "events.jsonl")
    sink.emit("complex", {"path": tmp_path, "raw": b"abc", "state": ComponentState.READY})
    record = json.loads((tmp_path / "events.jsonl").read_text(encoding="ascii"))
    assert record["payload"]["raw"] == "[BYTES:3]"


def test_metrics_reject_counter_gauge_collision():
    metrics = MetricsRegistry()
    metrics.increment("same")
    with pytest.raises(ValueError):
        metrics.gauge("same", 1)


def test_parity_verifier_calls_planner_for_each_surface():
    class CountingPlanner(R4HedgePlanner):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def plan(self, inputs):
            self.calls += 1
            return super().plan(inputs)

    planner = CountingPlanner()
    PlannerParityVerifier(planner).verify(
        R4PlanningInputs(context()),
        ("BACKTEST", "DRY_RUN", "TESTNET", "PRODUCTION_LOCKED"),
    )
    assert planner.calls == 4


def test_testnet_matrix_rejects_ambiguous_complete_text():
    class Orchestrator:
        async def run(self, **kwargs):
            return SimpleNamespace(
                final_state=TestnetScenarioState.COMPLETED,
                status="NOT_COMPLETE",
            )

    report = asyncio.run(
        R4TestnetMatrixRunner(Orchestrator()).run(
            credentials=object(),
            arm_token="x" * 16,
            actor="audit",
            execution_transport=object(),
        )
    )
    assert not report.passed


def test_forty_round_audit_has_exactly_40_passing_rounds(tmp_path):
    payload = run_forty_round_audit(project_root=Path.cwd(), output_dir=tmp_path)
    assert payload["audit_rounds"] == 40
    assert payload["failure_count"] == 0


def test_required_degraded_component_is_stopped_during_start_rollback():
    events = []

    class Component:
        def __init__(self, name, state):
            self.name = name
            self.required = True
            self.state = state

        def start(self):
            events.append(f"start:{self.name}")

        def stop(self):
            events.append(f"stop:{self.name}")
            self.state = ComponentState.STOPPED

        def health(self):
            return ComponentHealth(self.name, self.state)

    lifecycle = R4RuntimeLifecycle((Component("a", ComponentState.READY), Component("b", ComponentState.DEGRADED)))
    lifecycle.start()
    assert events[-2:] == ["stop:b", "stop:a"]


def test_non_adjacent_duplicate_cycle_key_is_blocked(tmp_path):
    config = runtime_config_from_mapping(
        {"mode": "PRODUCTION_LOCKED", "account_id": "binance-usdm:audit"},
        project_root=tmp_path,
    )
    runtime = R4ProductionRuntime(
        config=config,
        lifecycle=R4RuntimeLifecycle(tuple(StaticComponent(name) for name in NAMES)),
        context_provider=context,
    )
    runtime.start()
    assert runtime.run_once(cycle_key="bar-1").processed
    assert runtime.run_once(cycle_key="bar-2").processed
    assert runtime.run_once(cycle_key="bar-1").reason == "DUPLICATE_CYCLE"
