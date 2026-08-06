"""Docker object existence and lifecycle decisions without shell-text guessing."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any


class DockerInspectOutcome(StrEnum):
    EXISTS = "EXISTS"
    MISSING = "MISSING"
    QUERY_FAILED = "QUERY_FAILED"


class DockerLifecycleAction(StrEnum):
    NONE = "NONE"
    CREATE = "CREATE"
    START = "START"
    UNPAUSE = "UNPAUSE"
    WAIT_RESTART = "WAIT_RESTART"
    RECREATE = "RECREATE"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DockerState:
    outcome: DockerInspectOutcome
    status: str | None = None
    running: bool = False
    paused: bool = False
    restarting: bool = False
    dead: bool = False
    error: str | None = None

    @property
    def exists(self) -> bool:
        return self.outcome is DockerInspectOutcome.EXISTS


def classify_docker_inspect(*, returncode: int, stdout: str, stderr: str) -> DockerState:
    """Classify inspect as EXISTS/MISSING/QUERY_FAILED.

    Docker returns a non-zero status for both a missing object and daemon/query failures.
    The error text is therefore used only to distinguish the documented missing-object case;
    all other non-zero results remain hard failures.
    """

    if returncode == 0:
        try:
            payload = json.loads(stdout)
            item: dict[str, Any]
            if isinstance(payload, list):
                if not payload or not isinstance(payload[0], dict):
                    raise ValueError("inspect result did not contain an object")
                item = payload[0]
            elif isinstance(payload, dict):
                item = payload
            else:
                raise ValueError("inspect result was not JSON object/list")
            raw_state = item.get("State", {})
            state = raw_state if isinstance(raw_state, dict) else {}
            return DockerState(
                DockerInspectOutcome.EXISTS,
                status=str(state.get("Status", "unknown")).lower(),
                running=bool(state.get("Running", False)),
                paused=bool(state.get("Paused", False)),
                restarting=bool(state.get("Restarting", False)),
                dead=bool(state.get("Dead", False)),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return DockerState(
                DockerInspectOutcome.QUERY_FAILED,
                error=f"INVALID_INSPECT_JSON:{type(exc).__name__}:{exc}",
            )

    message = f"{stdout}\n{stderr}".strip()
    lowered = message.lower()
    missing_markers = (
        "no such container",
        "no such image",
        "no such object",
        "error: no such",
    )
    if any(marker in lowered for marker in missing_markers):
        return DockerState(DockerInspectOutcome.MISSING, error=message or None)
    return DockerState(DockerInspectOutcome.QUERY_FAILED, error=message or "docker inspect failed")


def lifecycle_action(state: DockerState, *, replace_dead: bool = False) -> DockerLifecycleAction:
    if state.outcome is DockerInspectOutcome.MISSING:
        return DockerLifecycleAction.CREATE
    if state.outcome is DockerInspectOutcome.QUERY_FAILED:
        return DockerLifecycleAction.FAIL
    if state.dead:
        return DockerLifecycleAction.RECREATE if replace_dead else DockerLifecycleAction.FAIL
    if state.paused:
        return DockerLifecycleAction.UNPAUSE
    if state.restarting or state.status == "restarting":
        return DockerLifecycleAction.WAIT_RESTART
    if state.running:
        return DockerLifecycleAction.NONE
    if state.status in {"created", "exited"}:
        return DockerLifecycleAction.START
    if state.status in {"removing", "dead"}:
        return DockerLifecycleAction.RECREATE if replace_dead else DockerLifecycleAction.FAIL
    return DockerLifecycleAction.FAIL


def safe_commit_arguments(container: str, image: str) -> tuple[str, ...]:
    """Return Docker commit arguments that never pause the source container."""

    if not container.strip() or not image.strip():
        raise ValueError("container and image are required")
    return ("commit", "--pause=false", container, image)
