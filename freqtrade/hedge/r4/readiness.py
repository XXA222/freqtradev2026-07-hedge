from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

from .models import ComponentHealth, ComponentState, R4Mode, RuntimePhase, RuntimeReadiness

_REQUIRED = (
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
_REQUIRED_SET = frozenset(_REQUIRED)


class ReadinessAggregator:
    def __init__(
        self,
        *,
        mode: R4Mode,
        max_age_seconds: Mapping[str, int] | None = None,
    ) -> None:
        self.mode = mode
        self.max_age_seconds = dict(max_age_seconds or {})
        if any(value <= 0 for value in self.max_age_seconds.values()):
            raise ValueError("readiness maximum ages must be positive")
        self._health: dict[str, ComponentHealth] = {
            name: ComponentHealth(name, ComponentState.UNKNOWN, True) for name in _REQUIRED
        }

    def update(self, health: ComponentHealth) -> None:
        name = health.name.strip()
        if not name:
            raise ValueError("component health name cannot be empty")
        # Core components are always required. A faulty adapter cannot downgrade them to optional.
        required = True if name in _REQUIRED_SET else health.required
        self._health[name] = ComponentHealth(
            name=name,
            state=health.state,
            required=required,
            detail=health.detail,
            updated_at=health.updated_at,
            metadata=health.metadata,
        )

    def update_many(self, values: Iterable[ComponentHealth]) -> None:
        for value in values:
            self.update(value)

    def snapshot(
        self,
        phase: RuntimePhase,
        *,
        now: datetime | None = None,
    ) -> RuntimeReadiness:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None or reference.utcoffset() is None:
            raise ValueError("readiness reference time must be timezone-aware")
        components: list[ComponentHealth] = []
        for name in sorted(self._health):
            item = self._health[name]
            max_age = self.max_age_seconds.get(name)
            if max_age is not None and reference - item.updated_at > timedelta(seconds=max_age):
                item = ComponentHealth(
                    item.name,
                    ComponentState.DEGRADED,
                    item.required,
                    f"stale for more than {max_age}s",
                    item.updated_at,
                    item.metadata,
                )
            components.append(item)
        blockers = tuple(
            f"{item.name}:{item.state.value}{':' + item.detail if item.detail else ''}"
            for item in components
            if item.required and not item.ready
        )
        ready = phase is RuntimePhase.READY and not blockers
        new_risk = ready and self.mode in {R4Mode.DRY_RUN, R4Mode.TESTNET}
        return RuntimeReadiness(
            phase=phase,
            ready=ready,
            new_risk_allowed=new_risk,
            blockers=blockers,
            components=tuple(components),
        )
