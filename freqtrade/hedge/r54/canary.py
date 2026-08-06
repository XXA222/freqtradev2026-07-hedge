from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class CanaryResult:
    request_count: int
    acknowledged_count: int
    terminal_count: int
    unknown_count: int
    max_notional: Decimal
    flat_finish: bool
    reconciliation_diff_count: int
    external_order_count: int
    recovery_required: bool = False

    def __post_init__(self) -> None:
        for name in (
            "request_count",
            "acknowledged_count",
            "terminal_count",
            "unknown_count",
            "reconciliation_diff_count",
            "external_order_count",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not self.max_notional.is_finite() or self.max_notional < 0:
            raise ValueError("max_notional must be finite and nonnegative")

    def passed(self, *, max_allowed_notional: Decimal, require_flat_finish: bool) -> bool:
        return (
            self.request_count > 0
            and self.acknowledged_count == self.request_count
            and self.terminal_count == self.request_count
            and self.unknown_count == 0
            and self.max_notional <= max_allowed_notional
            and (self.flat_finish or not require_flat_finish)
            and self.reconciliation_diff_count == 0
            and self.external_order_count == 0
            and not self.recovery_required
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "acknowledged_count": self.acknowledged_count,
            "terminal_count": self.terminal_count,
            "unknown_count": self.unknown_count,
            "max_notional": str(self.max_notional),
            "flat_finish": self.flat_finish,
            "reconciliation_diff_count": self.reconciliation_diff_count,
            "external_order_count": self.external_order_count,
            "recovery_required": self.recovery_required,
        }


def parse_canary_result(value: Mapping[str, Any]) -> CanaryResult:
    try:
        notional = Decimal(str(value["max_notional"]))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("invalid max_notional") from exc
    return CanaryResult(
        request_count=int(value["request_count"]),
        acknowledged_count=int(value["acknowledged_count"]),
        terminal_count=int(value["terminal_count"]),
        unknown_count=int(value["unknown_count"]),
        max_notional=notional,
        flat_finish=bool(value["flat_finish"]),
        reconciliation_diff_count=int(value["reconciliation_diff_count"]),
        external_order_count=int(value["external_order_count"]),
        recovery_required=bool(value.get("recovery_required", False)),
    )


def build_canary_evidence(
    *,
    kind: ArtifactKind,
    result: CanaryResult,
    account_id: str,
    environment: str,
    max_allowed_notional: Decimal,
    require_flat_finish: bool,
    producer: str = "r54-canary",
) -> EvidenceEnvelope:
    allowed = {
        ArtifactKind.TESTNET_ORDER_TEST,
        ArtifactKind.TESTNET_SUBMIT_CANCEL,
        ArtifactKind.TESTNET_SINGLE_LEG,
        ArtifactKind.TESTNET_DUAL_LEG,
        ArtifactKind.MAINNET_ORDER_TEST,
        ArtifactKind.MAINNET_SUBMIT_CANCEL,
        ArtifactKind.MAINNET_SINGLE_LEG,
        ArtifactKind.MAINNET_DUAL_LEG,
    }
    if kind not in allowed:
        raise ValueError("kind is not a canary evidence kind")
    passed = result.passed(
        max_allowed_notional=max_allowed_notional,
        require_flat_finish=require_flat_finish,
    )
    return create_envelope(
        kind=kind,
        account_id=account_id,
        environment=environment,
        producer=producer,
        payload={
            **result.to_payload(),
            "max_allowed_notional": str(max_allowed_notional),
            "require_flat_finish": require_flat_finish,
        },
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        ttl_seconds=24 * 3600,
        write_surface="ORDER_TEST" if "ORDER_TEST" in kind.value else "LIMITED_WRITE",
        real_order_requests=result.request_count,
    )
