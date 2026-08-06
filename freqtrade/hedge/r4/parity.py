from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from typing import Callable, Iterable, Mapping

from .planner import R4HedgePlanner, R4PlanningInputs


def _json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def planning_fingerprint(planner: R4HedgePlanner, inputs: R4PlanningInputs) -> str:
    decision = planner.plan(inputs)
    payload = {
        "result": asdict(decision.result),
        "config": asdict(decision.effective_config),
        "risk_flags": decision.risk_flags,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_value,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PlannerParityVerifier:
    """Execute every surface independently and compare the resulting fingerprints."""

    def __init__(
        self,
        planner: R4HedgePlanner | None = None,
        *,
        surface_planners: Mapping[str, Callable[[], R4HedgePlanner] | R4HedgePlanner] | None = None,
    ) -> None:
        self.planner = planner or R4HedgePlanner()
        self.surface_planners = {
            str(name).strip().upper(): value for name, value in (surface_planners or {}).items()
        }

    def _planner_for(self, surface: str) -> R4HedgePlanner:
        value = self.surface_planners.get(surface)
        if value is None:
            return self.planner
        planner = value() if callable(value) else value
        if not isinstance(planner, R4HedgePlanner):
            raise TypeError(f"surface {surface} did not provide an R4HedgePlanner")
        return planner

    def verify(self, inputs: R4PlanningInputs, surfaces: Iterable[str]) -> dict[str, str]:
        names = tuple(str(name).strip().upper() for name in surfaces)
        if not names or any(not name for name in names):
            raise ValueError("at least one non-empty planning surface is required")
        if len(names) != len(set(names)):
            raise ValueError("planning surfaces must be unique")
        results = {
            name: planning_fingerprint(self._planner_for(name), inputs)
            for name in names
        }
        if len(set(results.values())) != 1:
            details = ", ".join(f"{name}={digest}" for name, digest in results.items())
            raise AssertionError(f"planner parity violation: {details}")
        return results
