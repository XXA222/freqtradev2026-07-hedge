from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping

from freqtrade.hedge.r5.models import R5Stage

from .canonical import sha256_json


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PENDING = "PENDING"
    SKIP = "SKIP"


class GateStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    PENDING_EXTERNAL_EVIDENCE = "PENDING_EXTERNAL_EVIDENCE"


class ArtifactKind(StrEnum):
    MAINNET_READONLY_PREFLIGHT = "MAINNET_READONLY_PREFLIGHT"
    MAINNET_READONLY_24H = "MAINNET_READONLY_24H"
    MAINNET_READONLY_72H = "MAINNET_READONLY_72H"
    POSTGRES_MATRIX = "POSTGRES_MATRIX"
    BACKUP_RESTORE_DRILL = "BACKUP_RESTORE_DRILL"
    TESTNET_ORDER_TEST = "TESTNET_ORDER_TEST"
    TESTNET_SUBMIT_CANCEL = "TESTNET_SUBMIT_CANCEL"
    TESTNET_SINGLE_LEG = "TESTNET_SINGLE_LEG"
    TESTNET_DUAL_LEG = "TESTNET_DUAL_LEG"
    MAINNET_ORDER_TEST = "MAINNET_ORDER_TEST"
    MAINNET_SUBMIT_CANCEL = "MAINNET_SUBMIT_CANCEL"
    MAINNET_SINGLE_LEG = "MAINNET_SINGLE_LEG"
    MAINNET_DUAL_LEG = "MAINNET_DUAL_LEG"
    RESTRICTED_AUTO_24H = "RESTRICTED_AUTO_24H"
    RESTRICTED_AUTO_72H = "RESTRICTED_AUTO_72H"
    RESOURCE_SOAK = "RESOURCE_SOAK"
    ALERT_DRILL = "ALERT_DRILL"
    INCIDENT_DRILL = "INCIDENT_DRILL"
    CONFIG_VALIDATION = "CONFIG_VALIDATION"
    UPSTREAM_REBASE = "UPSTREAM_REBASE"
    RELEASE_DIAGNOSTICS = "RELEASE_DIAGNOSTICS"


READ_ONLY_KINDS = frozenset(
    {
        ArtifactKind.MAINNET_READONLY_PREFLIGHT,
        ArtifactKind.MAINNET_READONLY_24H,
        ArtifactKind.MAINNET_READONLY_72H,
        ArtifactKind.RESOURCE_SOAK,
        ArtifactKind.CONFIG_VALIDATION,
        ArtifactKind.UPSTREAM_REBASE,
        ArtifactKind.RELEASE_DIAGNOSTICS,
    }
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    reason: str = ""
    evidence_sha256: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status is CheckStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
            "evidence_sha256": self.evidence_sha256,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class EvidenceEnvelope:
    kind: ArtifactKind
    account_id: str
    environment: str
    status: CheckStatus
    created_at: datetime
    expires_at: datetime | None
    producer: str
    payload: Mapping[str, Any]
    payload_sha256: str
    write_surface: str = "NONE"
    real_order_requests: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise ValueError("expires_at must be timezone-aware")
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must follow created_at")
        if not self.account_id.strip():
            raise ValueError("account_id is required")
        if not self.environment.strip():
            raise ValueError("environment is required")
        if self.real_order_requests < 0:
            raise ValueError("real_order_requests must be nonnegative")
        actual = sha256_json(dict(self.payload))
        if actual != self.payload_sha256:
            raise ValueError("payload_sha256 does not match payload")
        if self.kind in READ_ONLY_KINDS:
            if self.write_surface.upper() != "NONE" or self.real_order_requests != 0:
                raise ValueError("read-only evidence may not contain exchange writes")

    @property
    def canonical_sha256(self) -> str:
        return sha256_json(self.to_dict(include_canonical=False))

    def is_fresh(self, now: datetime | None = None) -> bool:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        return self.expires_at is None or current < self.expires_at.astimezone(UTC)

    def to_dict(self, *, include_canonical: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "account_id": self.account_id,
            "environment": self.environment,
            "status": self.status.value,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "expires_at": (
                None if self.expires_at is None else self.expires_at.astimezone(UTC).isoformat()
            ),
            "producer": self.producer,
            "payload": dict(self.payload),
            "payload_sha256": self.payload_sha256,
            "write_surface": self.write_surface,
            "real_order_requests": self.real_order_requests,
        }
        if include_canonical:
            value["canonical_sha256"] = sha256_json(value)
        return value


@dataclass(frozen=True, slots=True)
class GateDecision:
    stage: R5Stage
    status: GateStatus
    checks: tuple[CheckResult, ...]
    evaluated_at: datetime
    account_id: str

    @property
    def ready(self) -> bool:
        return self.status is GateStatus.READY

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "stage": self.stage.value,
            "status": self.status.value,
            "evaluated_at": self.evaluated_at.astimezone(UTC).isoformat(),
            "account_id": self.account_id,
            "checks": [item.to_dict() for item in self.checks],
        }
        payload["canonical_sha256"] = sha256_json(payload)
        return payload
