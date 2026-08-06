from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class AutoObservationResult:
    duration_hours: float
    cycle_count: int
    write_request_count: int
    unknown_order_count: int
    non_ready_cycle_count: int
    reconciliation_diff_count: int
    budget_breach_count: int
    alert_count: int
    uncontained_incident_count: int
    realized_loss: Decimal
    maximum_drawdown: Decimal
    limit_realized_loss: Decimal
    limit_maximum_drawdown: Decimal

    def __post_init__(self) -> None:
        if self.duration_hours < 0:
            raise ValueError("duration_hours must be nonnegative")
        for name in (
            "cycle_count",
            "write_request_count",
            "unknown_order_count",
            "non_ready_cycle_count",
            "reconciliation_diff_count",
            "budget_breach_count",
            "alert_count",
            "uncontained_incident_count",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        for name in (
            "realized_loss",
            "maximum_drawdown",
            "limit_realized_loss",
            "limit_maximum_drawdown",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")

    def passed_for(self, minimum_hours: float) -> bool:
        return (
            self.duration_hours >= minimum_hours
            and self.cycle_count > 0
            and self.unknown_order_count == 0
            and self.non_ready_cycle_count == 0
            and self.reconciliation_diff_count == 0
            and self.budget_breach_count == 0
            and self.uncontained_incident_count == 0
            and self.realized_loss <= self.limit_realized_loss
            and self.maximum_drawdown <= self.limit_maximum_drawdown
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "duration_hours": self.duration_hours,
            "cycle_count": self.cycle_count,
            "write_request_count": self.write_request_count,
            "unknown_order_count": self.unknown_order_count,
            "non_ready_cycle_count": self.non_ready_cycle_count,
            "reconciliation_diff_count": self.reconciliation_diff_count,
            "budget_breach_count": self.budget_breach_count,
            "alert_count": self.alert_count,
            "uncontained_incident_count": self.uncontained_incident_count,
            "realized_loss": str(self.realized_loss),
            "maximum_drawdown": str(self.maximum_drawdown),
            "limit_realized_loss": str(self.limit_realized_loss),
            "limit_maximum_drawdown": str(self.limit_maximum_drawdown),
        }


def build_auto_observation_evidence(
    result: AutoObservationResult,
    *,
    account_id: str,
    minimum_hours: int,
) -> EvidenceEnvelope:
    if minimum_hours not in {24, 72}:
        raise ValueError("minimum_hours must be 24 or 72")
    kind = (
        ArtifactKind.RESTRICTED_AUTO_24H
        if minimum_hours == 24
        else ArtifactKind.RESTRICTED_AUTO_72H
    )
    return create_envelope(
        kind=kind,
        account_id=account_id,
        environment="MAINNET",
        producer="r54-auto-observation",
        payload={**result.to_payload(), "minimum_hours": minimum_hours},
        status=(
            CheckStatus.PASS if result.passed_for(float(minimum_hours)) else CheckStatus.FAIL
        ),
        ttl_seconds=7 * 24 * 3600,
        write_surface="LIMITED_WRITE",
        real_order_requests=result.write_request_count,
    )
