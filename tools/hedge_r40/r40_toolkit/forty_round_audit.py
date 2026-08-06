from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from freqtrade.hedge.execution.service import PositionSide as ExecutionPositionSide
from freqtrade.hedge.execution.testnet_e2e import TestnetScenarioState
from freqtrade.hedge.planning.context import (
    LegPosition,
    MarketSnapshot,
    PlannerConfig,
    PlanningContext,
    PositionSide,
    WalletSnapshot,
)
from freqtrade.hedge.r4 import __version__
from freqtrade.hedge.r4.config import runtime_config_from_mapping
from freqtrade.hedge.r4.diagnostics import DiagnosticRunner
from freqtrade.hedge.r4.lifecycle import R4RuntimeLifecycle
from freqtrade.hedge.r4.models import ComponentHealth, ComponentState, R4Mode, RuntimePhase
from freqtrade.hedge.r4.observability import JsonlEventSink, MetricsRegistry, redact
from freqtrade.hedge.r4.parity import PlannerParityVerifier
from freqtrade.hedge.r4.planner import R4HedgePlanner, R4PlanningInputs
from freqtrade.hedge.r4.readiness import ReadinessAggregator
from freqtrade.hedge.r4.runtime import R4ExecutionOutcome, R4ProductionRuntime, StaticComponent
from freqtrade.hedge.r4.simulation import R4ScenarioRunner, ScenarioDefinition
from freqtrade.hedge.r4.testnet_matrix import R4TestnetMatrixRunner
from freqtrade.hedge.simulation.exchange import BarEvent, SignalEvent


_NAMES = (
    "configuration",
    "database",
    "readonly_rest",
    "user_stream",
    "reconciliation",
    "planner",
    "risk",
    "execution",
    "control_plane",
    "supervisor",
)


def _context(*, timestamp: datetime | None = None) -> PlanningContext:
    return PlanningContext(
        market=MarketSnapshot(
            "ETHUSDT",
            timestamp or datetime.now(UTC),
            Decimal("1999.9"),
            Decimal("2000.1"),
            Decimal("2000"),
            Decimal("0.01"),
            Decimal("0.001"),
            Decimal("0.001"),
            Decimal("5"),
        ),
        wallet=WalletSnapshot(
            Decimal("1000"),
            Decimal("1000"),
            Decimal("1000"),
            LegPosition(PositionSide.LONG),
            LegPosition(PositionSide.SHORT),
            leverage=Decimal("3"),
        ),
        config=PlannerConfig(
            core_wallet_exposure_long=Decimal("0.01"),
            core_wallet_exposure_short=Decimal("0.01"),
            tactical_wallet_exposure_long=Decimal("0.005"),
            tactical_wallet_exposure_short=Decimal("0.005"),
            max_single_order_notional=Decimal("25"),
            cooldown_seconds=0,
        ),
        long_signal=Decimal("1"),
        short_signal=Decimal("1"),
    )


def _config(root: Path, mode: str = "PRODUCTION_LOCKED"):
    account = "binance-usdm-testnet:audit" if mode == "TESTNET" else "binance-usdm:audit"
    return runtime_config_from_mapping(
        {"hedge_r4": {"mode": mode, "account_id": account}}, project_root=root
    )


def _runtime(root: Path, *, mode: str = "PRODUCTION_LOCKED", context_provider=_context, handler=None):
    return R4ProductionRuntime(
        config=_config(root, mode),
        lifecycle=R4RuntimeLifecycle(tuple(StaticComponent(name) for name in _NAMES)),
        context_provider=context_provider,
        execution_handler=handler,
    )


class _Component:
    def __init__(self, name: str, events: list[str], *, fail_start=False, fail_stop=False, required=True):
        self.name = name
        self.required = required
        self.events = events
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.state = ComponentState.UNKNOWN

    def start(self):
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError("start failed")
        self.state = ComponentState.READY

    def stop(self):
        self.events.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("stop failed")
        self.state = ComponentState.STOPPED

    def health(self):
        return ComponentHealth(self.name, self.state, self.required)


class _Matrix:
    def __init__(self, status="TESTNET_E2E_COMPLETE", fail_token=""):
        self.status = status
        self.fail_token = fail_token
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_token and self.fail_token in kwargs["run_id"]:
            raise RuntimeError("synthetic failure")
        return SimpleNamespace(final_state=TestnetScenarioState.COMPLETED, status=self.status)


class _CountingPlanner(R4HedgePlanner):
    def __init__(self, *, divergent=False):
        super().__init__()
        self.calls = 0
        self.divergent = divergent

    def plan(self, inputs):
        self.calls += 1
        decision = super().plan(inputs)
        if self.divergent:
            return replace(decision, risk_flags=decision.risk_flags + ("DIVERGENT",))
        return decision


def _scenario() -> ScenarioDefinition:
    start = datetime(2026, 8, 3, tzinfo=UTC)
    events = [SignalEvent(start, "ETHUSDT", Decimal("1"), Decimal("1"), model_version="r401")]
    for i, value in enumerate(("2000", "1980", "2020", "1975", "2030"), 1):
        price = Decimal(value)
        events.append(
            BarEvent(
                start + timedelta(minutes=i),
                "ETHUSDT",
                price,
                price * Decimal("1.02"),
                price * Decimal("0.98"),
                price,
                Decimal("100"),
            )
        )
    return ScenarioDefinition("r401-audit", tuple(events), planner_config=_context().config)


def _expect_error(callback: Callable[[], object], expected: type[BaseException] = Exception) -> None:
    try:
        callback()
    except expected:
        return
    raise AssertionError(f"expected {expected.__name__}")


def run_forty_round_audit(*, project_root: Path, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checks: list[tuple[str, Callable[[], object]]] = []

    checks.append(("01_version_is_4_0_1", lambda: __version__ == "4.0.1" or (_ for _ in ()).throw(AssertionError(__version__))))
    checks.append(("02_string_false_is_false", lambda: not runtime_config_from_mapping({"planner_enabled": "false"}, project_root=project_root).planner_enabled or (_ for _ in ()).throw(AssertionError())))
    checks.append(("03_invalid_boolean_rejected", lambda: _expect_error(lambda: runtime_config_from_mapping({"planner_enabled": "maybe"}, project_root=project_root), ValueError)))
    checks.append(("04_symbol_allowlist_rejects_alias_abuse", lambda: _expect_error(lambda: runtime_config_from_mapping({"symbols": ["B/T/C/USDT"]}, project_root=project_root), ValueError)))
    checks.append(("05_duplicate_symbols_rejected", lambda: _expect_error(lambda: runtime_config_from_mapping({"symbols": ["BTCUSDT", "BTC/USDT"]}, project_root=project_root), ValueError)))
    checks.append(("06_account_namespace_strict", lambda: _expect_error(lambda: runtime_config_from_mapping({"account_id": "binance-usdm:"}, project_root=project_root), ValueError)))
    checks.append(("07_boolean_timing_rejected", lambda: _expect_error(lambda: runtime_config_from_mapping({"heartbeat_seconds": True}, project_root=project_root), ValueError)))
    checks.append(("08_nonfinite_cap_rejected", lambda: _expect_error(lambda: runtime_config_from_mapping({"max_single_order_notional": "NaN"}, project_root=project_root), ValueError)))

    def lifecycle_fail_closed():
        events: list[str] = []
        lifecycle = R4RuntimeLifecycle((_Component("a", events), _Component("b", events, fail_start=True), _Component("c", events)))
        transitions = lifecycle.start()
        assert lifecycle.phase is RuntimePhase.DEGRADED
        assert "start:c" not in events
        assert "stop:a" in events
        assert any(item.component == "c" and item.action == "skip" for item in transitions)
    checks.append(("09_lifecycle_required_failure_skips_downstream", lifecycle_fail_closed))

    def lifecycle_reverse_rollback():
        events: list[str] = []
        lifecycle = R4RuntimeLifecycle((_Component("a", events), _Component("b", events), _Component("c", events, fail_start=True)))
        lifecycle.start()
        assert events[-2:] == ["stop:b", "stop:a"]
    checks.append(("10_lifecycle_rollback_is_reverse_order", lifecycle_reverse_rollback))

    def stop_failure_phase():
        events: list[str] = []
        lifecycle = R4RuntimeLifecycle((_Component("a", events, fail_stop=True),))
        lifecycle.start(); lifecycle.stop()
        assert lifecycle.phase is RuntimePhase.FAILED
    checks.append(("11_stop_failure_is_not_reported_stopped", stop_failure_phase))

    def health_name_mismatch():
        component = _Component("a", [])
        component.health = lambda: ComponentHealth("wrong", ComponentState.READY)  # type: ignore[method-assign]
        lifecycle = R4RuntimeLifecycle((component,))
        assert lifecycle.health()[0].state is ComponentState.FAILED
    checks.append(("12_health_name_mismatch_fails_closed", health_name_mismatch))

    def required_cannot_downgrade():
        agg = ReadinessAggregator(mode=R4Mode.PRODUCTION_LOCKED)
        agg.update(ComponentHealth("database", ComponentState.FAILED, required=False))
        assert any(item.startswith("database:") for item in agg.snapshot(RuntimePhase.READY).blockers)
    checks.append(("13_core_required_flag_cannot_be_downgraded", required_cannot_downgrade))

    def stale_health():
        agg = ReadinessAggregator(mode=R4Mode.TESTNET, max_age_seconds={"user_stream": 5})
        for name in _NAMES:
            when = datetime.now(UTC) - timedelta(seconds=10) if name == "user_stream" else datetime.now(UTC)
            agg.update(ComponentHealth(name, ComponentState.READY, updated_at=when))
        assert any(item.startswith("user_stream:DEGRADED") for item in agg.snapshot(RuntimePhase.READY).blockers)
    checks.append(("14_stale_user_stream_blocks_readiness", stale_health))

    def locked_no_risk():
        agg = ReadinessAggregator(mode=R4Mode.PRODUCTION_LOCKED)
        agg.update_many(ComponentHealth(name, ComponentState.READY) for name in _NAMES)
        assert not agg.snapshot(RuntimePhase.READY).new_risk_allowed
    checks.append(("15_production_locked_never_allows_new_risk", locked_no_risk))

    checks.append(("16_volatility_expands_spacing", lambda: R4HedgePlanner().plan(R4PlanningInputs(_context(), realized_volatility=Decimal("0.02"))).effective_config.grid_spacing == Decimal("0.030") or (_ for _ in ()).throw(AssertionError())))
    checks.append(("17_positive_funding_suppresses_long_entries", lambda: all(item.position_side is not PositionSide.LONG or item.reduce_only for item in R4HedgePlanner().plan(R4PlanningInputs(_context(), funding_rate=Decimal("0.002"))).result.submit_orders) or (_ for _ in ()).throw(AssertionError())))
    checks.append(("18_runtime_notional_cap_is_applied", lambda: max((item.notional for item in R4HedgePlanner().plan(R4PlanningInputs(_context(), max_single_order_notional_cap=Decimal("10"))).result.ideal_orders), default=Decimal("0")) <= Decimal("10") or (_ for _ in ()).throw(AssertionError())))

    def submit_subset():
        decision = R4HedgePlanner().plan(R4PlanningInputs(_context()))
        ideal = {item.intent_id: item for item in decision.result.ideal_orders}
        assert all(ideal.get(item.intent_id) == item for item in decision.result.submit_orders)
    checks.append(("19_submit_orders_are_exact_ideal_members", submit_subset))

    def aligned_orders():
        decision = R4HedgePlanner().plan(R4PlanningInputs(_context()))
        assert all(item.quantity % _context().market.qty_step == 0 and item.price % _context().market.tick_size == 0 for item in decision.result.ideal_orders)
    checks.append(("20_order_tick_and_step_alignment", aligned_orders))

    def locked_handler_never_called():
        calls = []
        runtime = _runtime(project_root, handler=lambda result: calls.append(result))
        runtime.start(); report = runtime.run_once()
        assert report.writes_attempted == 0 and not calls
    checks.append(("21_locked_runtime_never_calls_execution_handler", locked_handler_never_called))

    def no_handler_no_fake_writes():
        runtime = _runtime(project_root, mode="DRY_RUN")
        runtime.start(); report = runtime.run_once()
        assert report.reason == "EXECUTION_HANDLER_UNAVAILABLE" and report.writes_attempted == 0 and not report.processed
    checks.append(("22_missing_handler_does_not_claim_writes", no_handler_no_fake_writes))

    def handler_actual_writes():
        runtime = _runtime(project_root, mode="DRY_RUN", handler=lambda result: R4ExecutionOutcome("SIMULATED", len(result.submit_orders) + len(result.cancel_order_ids)))
        runtime.start(); report = runtime.run_once()
        assert report.processed and report.writes_attempted == report.submissions_planned + report.cancellations_planned
    checks.append(("23_execution_handler_reports_actual_writes", handler_actual_writes))

    def stop_risk_filters_entries():
        captured = []
        runtime = _runtime(project_root, mode="DRY_RUN", handler=lambda result: (captured.append(result), R4ExecutionOutcome("SIMULATED", 0))[1])
        runtime.start(); runtime.stop_new_risk("audit"); report = runtime.run_once()
        assert captured and all(item.reduce_only for item in captured[0].submit_orders) and report.writes_blocked > 0
    checks.append(("24_stop_new_risk_filters_entries", stop_risk_filters_entries))

    def duplicate_cycle():
        runtime = _runtime(project_root); runtime.start()
        first = runtime.run_once(cycle_key="bar-1"); second = runtime.run_once(cycle_key="bar-1")
        assert first.processed and second.reason == "DUPLICATE_CYCLE" and not second.processed
    checks.append(("25_duplicate_cycle_key_is_blocked", duplicate_cycle))

    def stale_market():
        runtime = _runtime(project_root, context_provider=lambda: _context(timestamp=datetime.now(UTC) - timedelta(days=1)))
        runtime.start(); assert runtime.run_once().reason == "MARKET_DATA_STALE"
    checks.append(("26_stale_market_is_blocked", stale_market))

    def context_failure():
        runtime = _runtime(project_root, context_provider=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        runtime.start(); assert runtime.run_once().reason == "CONTEXT_PROVIDER_FAILED"
    checks.append(("27_context_failure_is_fail_closed", context_failure))

    def planner_failure():
        class BadPlanner(R4HedgePlanner):
            def plan(self, inputs): raise RuntimeError("boom")
        runtime = _runtime(project_root); runtime.planner = BadPlanner(); runtime.start()
        assert runtime.run_once().reason == "PLANNER_FAILED"
    checks.append(("28_planner_failure_is_fail_closed", planner_failure))

    def execution_failure():
        runtime = _runtime(project_root, mode="DRY_RUN", handler=lambda result: (_ for _ in ()).throw(RuntimeError("boom")))
        runtime.start(); report = runtime.run_once()
        assert report.reason == "EXECUTION_FAILED" and report.writes_attempted == 0
    checks.append(("29_execution_failure_is_fail_closed", execution_failure))

    def parity_invokes_each_surface():
        planner = _CountingPlanner()
        PlannerParityVerifier(planner).verify(R4PlanningInputs(_context()), ("BACKTEST", "DRY_RUN", "TESTNET", "PRODUCTION_LOCKED"))
        assert planner.calls == 4
    checks.append(("30_parity_executes_each_surface_independently", parity_invokes_each_surface))

    def parity_detects_divergence():
        verifier = PlannerParityVerifier(surface_planners={"TESTNET": _CountingPlanner(divergent=True)})
        _expect_error(lambda: verifier.verify(R4PlanningInputs(_context()), ("BACKTEST", "TESTNET")), AssertionError)
    checks.append(("31_parity_detects_divergent_surface", parity_detects_divergence))

    checks.append(("32_invalid_simulation_input_rejected", lambda: _expect_error(lambda: ScenarioDefinition("", ()), ValueError)))
    checks.append(("33_simulation_checkpoint_full_parity", lambda: R4ScenarioRunner().checkpoint_parity(_scenario(), 3) or (_ for _ in ()).throw(AssertionError())))

    def exact_matrix_status():
        report = asyncio.run(R4TestnetMatrixRunner(_Matrix(status="NOT_COMPLETE")).run(credentials=object(), arm_token="x" * 16, actor="audit", execution_transport=object()))
        assert not report.passed
    checks.append(("34_testnet_status_requires_exact_success", exact_matrix_status))

    def matrix_continues():
        matrix = _Matrix(fail_token="btcusdt-short")
        report = asyncio.run(R4TestnetMatrixRunner(matrix).run(credentials=object(), arm_token="x" * 16, actor="audit", execution_transport=object(), include_submit_cancel=True))
        assert len(matrix.calls) == 6 and len(report.failures) == 1
    checks.append(("35_testnet_matrix_continues_after_failure", matrix_continues))

    def string_redaction():
        value = redact({"url": "https://x.test/path?signature=abc&token=xyz", "header": "Bearer supersecret"})
        assert "abc" not in value["url"] and "xyz" not in value["url"] and "supersecret" not in value["header"]
    checks.append(("36_signed_url_and_bearer_are_redacted", string_redaction))

    def event_serialization():
        sink = JsonlEventSink(output_dir / "audit-events.jsonl")
        sink.emit("object", {"path": project_root, "bytes": b"secret", "side": ExecutionPositionSide.LONG})
        json.loads((output_dir / "audit-events.jsonl").read_text(encoding="ascii").splitlines()[-1])
    checks.append(("37_event_sink_serializes_non_json_types", event_serialization))

    def metric_collision():
        metrics = MetricsRegistry(); metrics.increment("same")
        _expect_error(lambda: metrics.gauge("same", 1), ValueError)
    checks.append(("38_metric_type_collision_rejected", metric_collision))

    def diagnostics_continue():
        runner = DiagnosticRunner(output_dir / "audit-diagnostics")
        runner.run_callable("fail", lambda: (_ for _ in ()).throw(RuntimeError("signature=abc")))
        runner.run_callable("continue", lambda: "ok")
        summary = runner.finish(datetime.now(UTC))
        assert len(summary.results) == 2 and summary.results[-1].status == "PASS"
        assert "abc" not in (summary.output_dir / "R4-DIAGNOSTIC-SUMMARY.json").read_text()
    checks.append(("39_diagnostics_continue_and_redact", diagnostics_continue))

    def static_safety_scan():
        roots = [project_root / "freqtrade" / "hedge" / "r4", project_root / "tools" / "hedge_r40"]
        findings = []
        patterns = [r"shell\s*=\s*True", r"mainnet_writes_enabled\s*=\s*True", r"HEDGE_PRODUCTION_ARMED"]
        for root in roots:
            for path in root.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if path.name in {"forty_round_audit.py", "full_diagnostics.py"}:
                    continue
                for pattern in patterns:
                    if re.search(pattern, text): findings.append(f"{path}:{pattern}")
        assert not findings, findings
    checks.append(("40_static_mainnet_and_shell_safety_scan", static_safety_scan))

    results = []
    for name, callback in checks:
        try:
            callback()
            results.append({"name": name, "status": "PASS", "detail": ""})
        except BaseException as exc:
            results.append({"name": name, "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"})
    failures = [item for item in results if item["status"] != "PASS"]
    payload = {
        "schema_version": 1,
        "toolkit_version": __version__,
        "audit_rounds": len(results),
        "status": "R4_40_ROUND_DEEP_AUDIT_PASS" if not failures else "R4_40_ROUND_DEEP_AUDIT_FAILED",
        "passed": not failures,
        "failure_count": len(failures),
        "results": results,
    }
    (output_dir / "R4-40-ROUND-DEEP-AUDIT.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "R4-40-ROUND-DEEP-AUDIT.txt").write_text("\n".join(f"[{item['status']}] {item['name']} {item['detail']}" for item in results) + "\n", encoding="utf-8")
    return payload
