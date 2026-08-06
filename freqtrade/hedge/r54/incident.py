from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


class IncidentState(StrEnum):
    OPEN = "OPEN"
    CONTAINED = "CONTAINED"
    RECOVERED = "RECOVERED"
    CLOSED = "CLOSED"


_ALLOWED = {
    IncidentState.OPEN: {IncidentState.CONTAINED},
    IncidentState.CONTAINED: {IncidentState.RECOVERED},
    IncidentState.RECOVERED: {IncidentState.CLOSED},
    IncidentState.CLOSED: set(),
}


@dataclass(slots=True)
class IncidentDrill:
    incident_id: str
    account_id: str
    state: IncidentState
    opened_at: datetime
    timeline: list[Mapping[str, Any]]

    @classmethod
    def open(cls, *, incident_id: str, account_id: str, reason: str) -> "IncidentDrill":
        now = datetime.now(UTC)
        return cls(
            incident_id=incident_id,
            account_id=account_id,
            state=IncidentState.OPEN,
            opened_at=now,
            timeline=[{"at": now.isoformat(), "state": "OPEN", "reason": reason}],
        )

    def transition(self, target: IncidentState, *, actor: str, note: str) -> None:
        if target not in _ALLOWED[self.state]:
            raise ValueError(f"invalid incident transition {self.state.value}->{target.value}")
        if not actor.strip() or not note.strip():
            raise ValueError("actor and note are required")
        now = datetime.now(UTC)
        self.state = target
        self.timeline.append(
            {"at": now.isoformat(), "state": target.value, "actor": actor, "note": note}
        )

    def evidence(self) -> EvidenceEnvelope:
        passed = self.state is IncidentState.CLOSED
        return create_envelope(
            kind=ArtifactKind.INCIDENT_DRILL,
            account_id=self.account_id,
            environment="LOCAL",
            producer="r54-incident-drill",
            payload={
                "incident_id": self.incident_id,
                "state": self.state.value,
                "opened_at": self.opened_at.isoformat(),
                "timeline": list(self.timeline),
            },
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            ttl_seconds=7 * 24 * 3600,
            write_surface="NONE",
        )
