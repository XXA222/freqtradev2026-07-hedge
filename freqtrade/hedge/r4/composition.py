from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from freqtrade.hedge.planning.context import PlanningContext

from .lifecycle import R4RuntimeLifecycle
from .models import ComponentHealth, ComponentState, R4RuntimeConfig
from .observability import JsonlEventSink, MetricsRegistry
from .runtime import R4ExecutionHandler, R4ProductionRuntime


@dataclass(slots=True)
class ComponentAdapter:
    name: str
    start_hook: Callable[[], None]
    stop_hook: Callable[[], None]
    health_hook: Callable[[], ComponentHealth | bool]
    required: bool = True

    def start(self) -> None:
        self.start_hook()

    def stop(self) -> None:
        self.stop_hook()

    def health(self) -> ComponentHealth:
        value = self.health_hook()
        if isinstance(value, ComponentHealth):
            if value.name != self.name:
                raise ValueError("component adapter health name mismatch")
            return value
        if not isinstance(value, bool):
            raise TypeError("component health hook must return ComponentHealth or bool")
        return ComponentHealth(
            self.name,
            ComponentState.READY if value else ComponentState.DEGRADED,
            self.required,
        )


@dataclass(frozen=True, slots=True)
class R4CompositionInputs:
    configuration: ComponentAdapter
    database: ComponentAdapter
    readonly_rest: ComponentAdapter
    user_stream: ComponentAdapter
    reconciliation: ComponentAdapter
    planner: ComponentAdapter
    risk: ComponentAdapter
    execution: ComponentAdapter
    control_plane: ComponentAdapter
    supervisor: ComponentAdapter
    context_provider: Callable[[], PlanningContext]
    execution_handler: R4ExecutionHandler | None = None

    def ordered_components(self) -> tuple[ComponentAdapter, ...]:
        expected = (
            ("configuration", self.configuration),
            ("database", self.database),
            ("readonly_rest", self.readonly_rest),
            ("user_stream", self.user_stream),
            ("reconciliation", self.reconciliation),
            ("planner", self.planner),
            ("risk", self.risk),
            ("execution", self.execution),
            ("control_plane", self.control_plane),
            ("supervisor", self.supervisor),
        )
        for name, adapter in expected:
            if adapter.name != name:
                raise ValueError(f"R4 component {name} was bound as {adapter.name!r}")
        return tuple(adapter for _, adapter in expected)


def build_r4_runtime(
    *,
    config: R4RuntimeConfig,
    inputs: R4CompositionInputs,
) -> R4ProductionRuntime:
    state_dir = config.resolved_state_dir()
    return R4ProductionRuntime(
        config=config,
        lifecycle=R4RuntimeLifecycle(inputs.ordered_components()),
        context_provider=inputs.context_provider,
        execution_handler=inputs.execution_handler,
        metrics=MetricsRegistry(),
        events=JsonlEventSink(config.events_path or state_dir / "events.jsonl"),
    )
