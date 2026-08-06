from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Callable, Protocol
from uuid import uuid4

from freqtrade.hedge.planning.context import PlanningContext, PlanningResult

from .lifecycle import R4RuntimeLifecycle
from .models import (
    ComponentHealth,
    ComponentState,
    R4CycleReport,
    R4Mode,
    R4RuntimeConfig,
    RuntimeReadiness,
)
from .observability import JsonlEventSink, MetricsRegistry
from .planner import R4HedgePlanner, R4PlanningInputs
from .readiness import ReadinessAggregator


@dataclass(frozen=True, slots=True)
class R4ExecutionOutcome:
    status: str
    writes_attempted: int
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("execution outcome status cannot be empty")
        if self.writes_attempted < 0:
            raise ValueError("execution writes_attempted cannot be negative")


class R4ExecutionHandler(Protocol):
    def __call__(self, result: PlanningResult) -> R4ExecutionOutcome: ...


@dataclass(slots=True)
class StaticComponent:
    name: str
    required: bool = True
    state: ComponentState = ComponentState.UNKNOWN
    detail: str = ""

    def start(self) -> None:
        self.state = ComponentState.READY

    def stop(self) -> None:
        self.state = ComponentState.STOPPED

    def health(self) -> ComponentHealth:
        return ComponentHealth(self.name, self.state, self.required, self.detail)


class R4ProductionRuntime:
    def __init__(
        self,
        *,
        config: R4RuntimeConfig,
        lifecycle: R4RuntimeLifecycle,
        context_provider: Callable[[], PlanningContext],
        planner: R4HedgePlanner | None = None,
        execution_handler: R4ExecutionHandler | None = None,
        metrics: MetricsRegistry | None = None,
        events: JsonlEventSink | None = None,
    ) -> None:
        self.config = config
        self.lifecycle = lifecycle
        self.context_provider = context_provider
        self.planner = planner or R4HedgePlanner()
        self.execution_handler = execution_handler
        self.metrics = metrics or MetricsRegistry()
        self.events = events
        self.readiness_aggregator = ReadinessAggregator(
            mode=config.mode,
            max_age_seconds={
                "user_stream": config.user_stream_max_age_seconds,
                "reconciliation": config.reconciliation_max_age_seconds,
            },
        )
        self._cycle_sequence = 0
        self._instance_nonce = uuid4().hex
        self._processed_cycle_keys: set[str] = set()
        self._processed_cycle_order: deque[str] = deque()
        self._processed_cycle_limit = 4096
        self._new_risk_stopped = False

    def start(self) -> RuntimeReadiness:
        transitions = self.lifecycle.start()
        self.readiness_aggregator.update_many(self.lifecycle.health())
        snapshot = self.readiness()
        self.metrics.increment("r4_runtime_start_total")
        self.metrics.gauge("r4_runtime_ready", 1 if snapshot.ready else 0)
        if self.events:
            self.events.emit(
                "runtime_started",
                {
                    "phase": snapshot.phase.value,
                    "blockers": snapshot.blockers,
                    "transitions": [asdict(item) for item in transitions],
                },
            )
        return snapshot

    def stop(self) -> RuntimeReadiness:
        transitions = self.lifecycle.stop()
        self.readiness_aggregator.update_many(self.lifecycle.health())
        self.metrics.increment("r4_runtime_stop_total")
        if self.events:
            self.events.emit(
                "runtime_stopped",
                {"transitions": [asdict(item) for item in transitions]},
            )
        return self.readiness()

    def readiness(self) -> RuntimeReadiness:
        self.readiness_aggregator.update_many(self.lifecycle.health())
        return self.readiness_aggregator.snapshot(self.lifecycle.phase)

    def stop_new_risk(self, reason: str) -> None:
        normalized = str(reason).strip()
        if not normalized:
            raise ValueError("new-risk stop reason cannot be empty")
        self._new_risk_stopped = True
        self.metrics.increment("r4_new_risk_stop_total")
        if self.events:
            self.events.emit("new_risk_stopped", {"reason": normalized})

    def resume_new_risk(self) -> None:
        if not self.readiness().ready:
            raise RuntimeError("cannot resume new risk while runtime is not ready")
        self._new_risk_stopped = False
        self.metrics.increment("r4_new_risk_resume_total")
        if self.events:
            self.events.emit("new_risk_resumed", {})

    def _report_blocked(
        self,
        cycle_id: str,
        reason: str,
        diagnostics: tuple[str, ...],
    ) -> R4CycleReport:
        self.metrics.increment("r4_cycle_blocked_total")
        return R4CycleReport(
            cycle_id=cycle_id,
            processed=False,
            reason=reason,
            diagnostics=diagnostics,
        )

    def run_once(
        self,
        *,
        realized_volatility: Decimal | int | float | str = 0,
        funding_rate: Decimal | int | float | str = 0,
        cycle_key: str | None = None,
    ) -> R4CycleReport:
        self._cycle_sequence += 1
        normalized_cycle_key = None if cycle_key is None else str(cycle_key).strip()
        if cycle_key is not None and not normalized_cycle_key:
            raise ValueError("cycle_key cannot be empty")
        identity = normalized_cycle_key or f"{self._instance_nonce}:{self._cycle_sequence}"
        cycle_id = sha256(f"{self.config.account_id}|{identity}".encode("utf-8")).hexdigest()[:24]
        if normalized_cycle_key is not None and normalized_cycle_key in self._processed_cycle_keys:
            return self._report_blocked(cycle_id, "DUPLICATE_CYCLE", ("cycle key already processed",))

        readiness = self.readiness()
        if not readiness.ready:
            self.metrics.increment("r4_cycle_blocked_readiness_total")
            return self._report_blocked(cycle_id, "READINESS_BLOCKED", readiness.blockers)

        try:
            context = self.context_provider()
        except Exception as exc:
            self.stop_new_risk("context provider failed")
            return self._report_blocked(
                cycle_id,
                "CONTEXT_PROVIDER_FAILED",
                (f"{type(exc).__name__}: {exc}",),
            )
        if context.market.symbol not in self.config.symbols:
            self.stop_new_risk("context symbol outside configured allowlist")
            return self._report_blocked(
                cycle_id,
                "SYMBOL_NOT_ALLOWED",
                (context.market.symbol,),
            )
        market_age = datetime.now(UTC) - context.market.timestamp
        max_market_age = timedelta(seconds=self.config.market_data_max_age_seconds)
        if market_age < timedelta(0) or market_age > max_market_age:
            self.stop_new_risk("market data is stale or from the future")
            return self._report_blocked(
                cycle_id,
                "MARKET_DATA_STALE",
                (f"market_age_seconds={market_age.total_seconds():.3f}",),
            )

        try:
            decision = self.planner.plan(
                R4PlanningInputs(
                    context=context,
                    realized_volatility=Decimal(str(realized_volatility)),
                    funding_rate=Decimal(str(funding_rate)),
                    max_single_order_notional_cap=self.config.max_single_order_notional,
                    max_gross_wallet_exposure_cap=self.config.max_gross_wallet_exposure,
                )
            )
        except Exception as exc:
            self.stop_new_risk("planner failed")
            return self._report_blocked(
                cycle_id,
                "PLANNER_FAILED",
                (f"{type(exc).__name__}: {exc}",),
            )

        planned_submits = len(decision.result.submit_orders)
        planned_cancels = len(decision.result.cancel_order_ids)
        blocked = 0
        writes = 0
        reason = "PLANNED"
        execution_diagnostics: tuple[str, ...] = ()

        if self.config.mode is R4Mode.PRODUCTION_LOCKED:
            blocked = planned_submits + planned_cancels
            reason = "PRODUCTION_LOCKED_PLAN_ONLY"
        else:
            executable = decision.result
            if self._new_risk_stopped:
                allowed_submits = tuple(item for item in executable.submit_orders if item.reduce_only)
                blocked = len(executable.submit_orders) - len(allowed_submits)
                executable = replace(executable, submit_orders=allowed_submits)
                reason = "NEW_RISK_STOPPED"
            if self.execution_handler is None:
                blocked += len(executable.submit_orders) + len(executable.cancel_order_ids)
                reason = "EXECUTION_HANDLER_UNAVAILABLE"
            else:
                try:
                    outcome = self.execution_handler(executable)
                    max_actions = len(executable.submit_orders) + len(executable.cancel_order_ids)
                    if outcome.writes_attempted > max_actions:
                        raise ValueError("execution handler reported more writes than planned actions")
                    writes = outcome.writes_attempted
                    execution_diagnostics = outcome.diagnostics + (f"EXECUTION:{outcome.status}",)
                except Exception as exc:
                    self.stop_new_risk("execution handler failed")
                    reason = "EXECUTION_FAILED"
                    blocked += len(executable.submit_orders) + len(executable.cancel_order_ids)
                    execution_diagnostics = (f"{type(exc).__name__}: {exc}",)

        self.metrics.increment("r4_cycle_total")
        self.metrics.increment("r4_planned_submit_total", planned_submits)
        self.metrics.increment("r4_planned_cancel_total", planned_cancels)
        self.metrics.increment("r4_blocked_write_total", blocked)
        self.metrics.increment("r4_exchange_write_attempt_total", writes)
        if self.config.mode is R4Mode.PRODUCTION_LOCKED and writes != 0:
            raise AssertionError("production locked runtime attempted exchange writes")

        report = R4CycleReport(
            cycle_id=cycle_id,
            processed=reason not in {"EXECUTION_HANDLER_UNAVAILABLE", "EXECUTION_FAILED"},
            reason=reason,
            planning_result=decision.result,
            writes_attempted=writes,
            writes_blocked=blocked,
            cancellations_planned=planned_cancels,
            submissions_planned=planned_submits,
            diagnostics=decision.risk_flags + decision.result.diagnostics + execution_diagnostics,
        )
        if report.processed and normalized_cycle_key is not None:
            self._processed_cycle_keys.add(normalized_cycle_key)
            self._processed_cycle_order.append(normalized_cycle_key)
            while len(self._processed_cycle_order) > self._processed_cycle_limit:
                expired = self._processed_cycle_order.popleft()
                self._processed_cycle_keys.discard(expired)
        if self.events:
            self.events.emit(
                "cycle_completed",
                {
                    "cycle_id": cycle_id,
                    "cycle_key": normalized_cycle_key,
                    "reason": reason,
                    "processed": report.processed,
                    "writes_attempted": writes,
                    "writes_blocked": blocked,
                },
            )
        return report
