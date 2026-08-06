from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping

from freqtrade.hedge.r5.models import R5Stage

from .models import ArtifactKind, CheckResult, CheckStatus, GateDecision, GateStatus
from .store import R54OperationalStore


@dataclass(frozen=True, slots=True)
class StagePolicy:
    stage: R5Stage
    required: tuple[ArtifactKind, ...]


@dataclass(frozen=True, slots=True)
class EvidenceRule:
    environment: str
    write_surfaces: frozenset[str]
    minimum_requests: int = 0


EVIDENCE_RULES: Mapping[ArtifactKind, EvidenceRule] = {
    ArtifactKind.MAINNET_READONLY_PREFLIGHT: EvidenceRule("MAINNET", frozenset({"NONE"})),
    ArtifactKind.MAINNET_READONLY_24H: EvidenceRule("MAINNET", frozenset({"NONE"})),
    ArtifactKind.MAINNET_READONLY_72H: EvidenceRule("MAINNET", frozenset({"NONE"})),
    ArtifactKind.POSTGRES_MATRIX: EvidenceRule("LOCAL_POSTGRES", frozenset({"NONE"})),
    ArtifactKind.BACKUP_RESTORE_DRILL: EvidenceRule("LOCAL", frozenset({"NONE"})),
    ArtifactKind.TESTNET_ORDER_TEST: EvidenceRule(
        "TESTNET", frozenset({"ORDER_TEST"}), minimum_requests=1
    ),
    ArtifactKind.TESTNET_SUBMIT_CANCEL: EvidenceRule(
        "TESTNET", frozenset({"LIMITED_WRITE"}), minimum_requests=1
    ),
    ArtifactKind.TESTNET_SINGLE_LEG: EvidenceRule(
        "TESTNET", frozenset({"LIMITED_WRITE"}), minimum_requests=1
    ),
    ArtifactKind.TESTNET_DUAL_LEG: EvidenceRule(
        "TESTNET", frozenset({"LIMITED_WRITE"}), minimum_requests=1
    ),
    ArtifactKind.MAINNET_ORDER_TEST: EvidenceRule(
        "MAINNET", frozenset({"ORDER_TEST"}), minimum_requests=1
    ),
    ArtifactKind.MAINNET_SUBMIT_CANCEL: EvidenceRule(
        "MAINNET", frozenset({"LIMITED_WRITE"}), minimum_requests=1
    ),
    ArtifactKind.MAINNET_SINGLE_LEG: EvidenceRule(
        "MAINNET", frozenset({"LIMITED_WRITE"}), minimum_requests=1
    ),
    ArtifactKind.MAINNET_DUAL_LEG: EvidenceRule(
        "MAINNET", frozenset({"LIMITED_WRITE"}), minimum_requests=1
    ),
    ArtifactKind.RESTRICTED_AUTO_24H: EvidenceRule(
        "MAINNET", frozenset({"LIMITED_WRITE"})
    ),
    ArtifactKind.RESTRICTED_AUTO_72H: EvidenceRule(
        "MAINNET", frozenset({"LIMITED_WRITE"})
    ),
    ArtifactKind.RESOURCE_SOAK: EvidenceRule("LOCAL", frozenset({"NONE"})),
    ArtifactKind.ALERT_DRILL: EvidenceRule("LOCAL", frozenset({"NONE"})),
    ArtifactKind.INCIDENT_DRILL: EvidenceRule("LOCAL", frozenset({"NONE"})),
    ArtifactKind.CONFIG_VALIDATION: EvidenceRule("SOURCE", frozenset({"NONE"})),
    ArtifactKind.UPSTREAM_REBASE: EvidenceRule("SOURCE", frozenset({"NONE"})),
    ArtifactKind.RELEASE_DIAGNOSTICS: EvidenceRule("LOCAL", frozenset({"NONE"})),
}


POLICIES: Mapping[R5Stage, StagePolicy] = {
    R5Stage.MAINNET_READONLY: StagePolicy(
        R5Stage.MAINNET_READONLY,
        (ArtifactKind.MAINNET_READONLY_PREFLIGHT,),
    ),
    R5Stage.ORDER_TEST: StagePolicy(
        R5Stage.ORDER_TEST,
        (
            ArtifactKind.MAINNET_READONLY_24H,
            ArtifactKind.MAINNET_READONLY_72H,
            ArtifactKind.POSTGRES_MATRIX,
            ArtifactKind.BACKUP_RESTORE_DRILL,
            ArtifactKind.CONFIG_VALIDATION,
            ArtifactKind.UPSTREAM_REBASE,
            ArtifactKind.RELEASE_DIAGNOSTICS,
            ArtifactKind.TESTNET_ORDER_TEST,
        ),
    ),
    R5Stage.SUBMIT_CANCEL: StagePolicy(
        R5Stage.SUBMIT_CANCEL,
        (ArtifactKind.MAINNET_ORDER_TEST, ArtifactKind.TESTNET_SUBMIT_CANCEL),
    ),
    R5Stage.SINGLE_LEG: StagePolicy(
        R5Stage.SINGLE_LEG,
        (ArtifactKind.MAINNET_SUBMIT_CANCEL, ArtifactKind.TESTNET_SINGLE_LEG),
    ),
    R5Stage.DUAL_LEG: StagePolicy(
        R5Stage.DUAL_LEG,
        (ArtifactKind.MAINNET_SINGLE_LEG, ArtifactKind.TESTNET_DUAL_LEG),
    ),
    R5Stage.RESTRICTED_AUTO: StagePolicy(
        R5Stage.RESTRICTED_AUTO,
        (
            ArtifactKind.MAINNET_DUAL_LEG,
            ArtifactKind.ALERT_DRILL,
            ArtifactKind.INCIDENT_DRILL,
            ArtifactKind.RESOURCE_SOAK,
        ),
    ),
    R5Stage.EXPANDED_LIMITS: StagePolicy(
        R5Stage.EXPANDED_LIMITS,
        (ArtifactKind.RESTRICTED_AUTO_24H, ArtifactKind.RESTRICTED_AUTO_72H),
    ),
}


class R54PromotionEngine:
    def __init__(self, store: R54OperationalStore) -> None:
        self.store = store

    def evaluate(
        self,
        *,
        account_id: str,
        stage: R5Stage,
        now: datetime | None = None,
    ) -> GateDecision:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        checks: list[CheckResult] = []
        required: list[ArtifactKind] = []
        for candidate in R5Stage:
            if candidate.rank > stage.rank:
                break
            for kind in POLICIES[candidate].required:
                if kind not in required:
                    required.append(kind)
        for kind in required:
            item = self.store.latest_evidence(account_id=account_id, kind=kind)
            if item is None:
                checks.append(
                    CheckResult(
                        name=kind.value,
                        status=CheckStatus.PENDING,
                        reason="required evidence has not been recorded",
                    )
                )
                continue
            if item.status is not CheckStatus.PASS:
                checks.append(
                    CheckResult(
                        name=kind.value,
                        status=CheckStatus.FAIL,
                        reason="latest evidence status is not PASS",
                        evidence_sha256=item.canonical_sha256,
                    )
                )
                continue
            if not item.is_fresh(current):
                checks.append(
                    CheckResult(
                        name=kind.value,
                        status=CheckStatus.FAIL,
                        reason="latest evidence is stale",
                        evidence_sha256=item.canonical_sha256,
                    )
                )
                continue
            rule = EVIDENCE_RULES[kind]
            if item.environment != rule.environment:
                checks.append(
                    CheckResult(
                        name=kind.value,
                        status=CheckStatus.FAIL,
                        reason=(
                            f"evidence environment {item.environment} does not match "
                            f"{rule.environment}"
                        ),
                        evidence_sha256=item.canonical_sha256,
                    )
                )
                continue
            if item.write_surface not in rule.write_surfaces:
                checks.append(
                    CheckResult(
                        name=kind.value,
                        status=CheckStatus.FAIL,
                        reason=f"evidence write surface {item.write_surface} is not allowed",
                        evidence_sha256=item.canonical_sha256,
                    )
                )
                continue
            if item.real_order_requests < rule.minimum_requests:
                checks.append(
                    CheckResult(
                        name=kind.value,
                        status=CheckStatus.FAIL,
                        reason="evidence request count is below the stage minimum",
                        evidence_sha256=item.canonical_sha256,
                    )
                )
                continue
            checks.append(
                CheckResult(
                    name=kind.value,
                    status=CheckStatus.PASS,
                    evidence_sha256=item.canonical_sha256,
                )
            )
        if all(item.status is CheckStatus.PASS for item in checks):
            status = GateStatus.READY
        elif any(item.status is CheckStatus.FAIL for item in checks):
            status = GateStatus.BLOCKED
        else:
            status = GateStatus.PENDING_EXTERNAL_EVIDENCE
        decision = GateDecision(
            stage=stage,
            status=status,
            checks=tuple(checks),
            evaluated_at=current,
            account_id=account_id,
        )
        self.store.put_decision(decision)
        return decision
