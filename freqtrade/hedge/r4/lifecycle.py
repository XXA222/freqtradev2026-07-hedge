from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .models import ComponentHealth, ComponentState, RuntimePhase


@runtime_checkable
class RuntimeComponent(Protocol):
    name: str
    required: bool

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> ComponentHealth: ...


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    component: str
    action: str
    success: bool
    detail: str = ""


class R4RuntimeLifecycle:
    """Dependency-ordered, fail-closed lifecycle.

    Diagnostic suites continue after failures; the trading runtime must not. Once a required
    component fails, downstream components are skipped and already-started components are
    stopped in reverse order.
    """

    def __init__(self, components: tuple[RuntimeComponent, ...]) -> None:
        names = [item.name for item in components]
        if not components:
            raise ValueError("runtime requires at least one component")
        if any(not name or not name.strip() for name in names):
            raise ValueError("runtime component names cannot be empty")
        if len(names) != len(set(names)):
            raise ValueError("runtime component names must be unique")
        self.components = components
        self.started: list[RuntimeComponent] = []
        self.phase = RuntimePhase.CREATED
        self.transitions: list[LifecycleTransition] = []

    def start(self) -> tuple[LifecycleTransition, ...]:
        if self.phase not in {RuntimePhase.CREATED, RuntimePhase.STOPPED}:
            raise RuntimeError(f"cannot start lifecycle from {self.phase.value}")
        self.phase = RuntimePhase.STARTING
        self.transitions.clear()
        self.started.clear()
        fatal = False
        for component in self.components:
            if fatal:
                self.transitions.append(
                    LifecycleTransition(
                        component.name,
                        "skip",
                        False,
                        "skipped after required component failure",
                    )
                )
                continue
            try:
                component.start()
                self.started.append(component)
                health = component.health()
                if health.name != component.name:
                    raise RuntimeError("component health name mismatch")
                ok = health.state is ComponentState.READY
                self.transitions.append(
                    LifecycleTransition(component.name, "start", ok, health.detail)
                )
                if component.required and not ok:
                    fatal = True
            except Exception as exc:
                self.transitions.append(
                    LifecycleTransition(
                        component.name,
                        "start",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                if component.required:
                    fatal = True

        if fatal:
            self._rollback_started()
            self.phase = RuntimePhase.DEGRADED
        else:
            self.phase = RuntimePhase.READY
        return tuple(self.transitions)

    def _rollback_started(self) -> None:
        for component in reversed(self.started):
            try:
                component.stop()
                self.transitions.append(
                    LifecycleTransition(component.name, "rollback-stop", True)
                )
            except Exception as exc:
                self.transitions.append(
                    LifecycleTransition(
                        component.name,
                        "rollback-stop",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        self.started.clear()

    def stop(self) -> tuple[LifecycleTransition, ...]:
        if self.phase is RuntimePhase.STOPPED:
            return ()
        self.phase = RuntimePhase.STOPPING
        transitions: list[LifecycleTransition] = []
        failed = False
        for component in reversed(self.started):
            try:
                component.stop()
                transitions.append(LifecycleTransition(component.name, "stop", True))
            except Exception as exc:
                failed = True
                transitions.append(
                    LifecycleTransition(
                        component.name,
                        "stop",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        self.started.clear()
        self.phase = RuntimePhase.FAILED if failed else RuntimePhase.STOPPED
        self.transitions.extend(transitions)
        return tuple(transitions)

    def health(self) -> tuple[ComponentHealth, ...]:
        values: list[ComponentHealth] = []
        for component in self.components:
            try:
                health = component.health()
                if health.name != component.name:
                    raise RuntimeError("component health name mismatch")
                values.append(health)
            except Exception as exc:
                values.append(
                    ComponentHealth(
                        component.name,
                        ComponentState.FAILED,
                        component.required,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        return tuple(values)
