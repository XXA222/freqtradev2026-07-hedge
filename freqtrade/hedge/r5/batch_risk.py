"""Atomic batch-risk handoff for the authoritative R5 execution path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Iterable

from freqtrade.enums.hedge import PositionAction as RiskAction
from freqtrade.enums.hedge import PositionSide as RiskSide
from freqtrade.hedge.execution.service import OrderIntent, RiskApproval
from freqtrade.hedge.risk.commit import RiskApprovalCommitPort
from freqtrade.hedge.risk.portfolio import RiskPortfolioSnapshot
from freqtrade.hedge.risk.runtime import HedgeRiskRuntime, OrderRiskIntent


def _reference_price(intent: OrderIntent) -> Decimal | None:
    if intent.limit_price is not None:
        return intent.limit_price
    raw = intent.metadata.get("reference_price")
    return None if raw is None else Decimal(str(raw))


def _expires_at_ms(intent: OrderIntent) -> int | None:
    value = intent.metadata.get("expires_at")
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("INTENT_EXPIRY_INVALID")
    return int(value.timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class R5PreparedRiskBatch:
    intent_keys: tuple[str, ...]
    decision_ids: tuple[str, ...]
    fencing_token: int
    risk_snapshot_id: str


class R5PreparedBatchRiskApproval:
    """ExecutionService risk port backed by a pre-approved atomic batch.

    The caller must invoke :meth:`prepare` before the first exchange write. All risk
    reservations are evaluated against the same authoritative portfolio with cumulative
    pending-risk projection and are handed to SQL in one durable commit. ``approve`` then
    only releases the exact pre-approved quantity to ExecutionService; an unprepared or
    altered intent is denied fail-closed.
    """

    def __init__(
        self,
        *,
        runtime: HedgeRiskRuntime,
        commit_store: RiskApprovalCommitPort,
        leverage: Decimal,
        maintenance_margin_rate: Decimal,
    ) -> None:
        if leverage < 1 or not leverage.is_finite():
            raise ValueError("leverage must be finite and >= 1")
        if (
            not maintenance_margin_rate.is_finite()
            or maintenance_margin_rate <= 0
            or maintenance_margin_rate > 1
        ):
            raise ValueError("maintenance_margin_rate must be in (0, 1]")
        self._runtime = runtime
        self._commit_store = commit_store
        self._leverage = leverage
        self._maintenance_margin_rate = maintenance_margin_rate
        self._prepared: dict[str, tuple[OrderIntent, RiskApproval]] = {}
        self._lock = RLock()

    @staticmethod
    def _signature(intent: OrderIntent) -> str:
        return "|".join(
            (
                intent.account_id,
                intent.symbol,
                intent.position_side.value,
                intent.action.value,
                str(intent.quantity),
                intent.order_type.value,
                "" if intent.limit_price is None else str(intent.limit_price),
                str(bool(intent.reduce_only)),
                intent.idempotency_key,
                str(intent.intent_id),
            )
        )

    def clear(self) -> None:
        with self._lock:
            self._prepared.clear()

    def prepare(
        self,
        *,
        portfolio: RiskPortfolioSnapshot,
        intents: Iterable[OrderIntent],
        expected_fencing_token: int,
        durable_namespace: str,
    ) -> R5PreparedRiskBatch:
        values = tuple(intents)
        if not values:
            raise ValueError("R5 risk batch must not be empty")
        if len({item.idempotency_key for item in values}) != len(values):
            raise ValueError("R5 risk batch idempotency keys must be unique")
        risk_intents: list[OrderRiskIntent] = []
        for intent in values:
            reference = _reference_price(intent)
            if reference is None or not reference.is_finite() or reference <= 0:
                raise PermissionError("REFERENCE_PRICE_REQUIRED")
            risk_intents.append(
                OrderRiskIntent(
                    symbol=intent.symbol,
                    position_side=RiskSide(intent.position_side.value),
                    action=RiskAction(intent.action.value),
                    requested_quantity=intent.quantity,
                    reference_price=reference,
                    leverage=self._leverage,
                    intent_id=str(intent.intent_id),
                    idempotency_key=intent.idempotency_key,
                    correlation_id=str(intent.action_group_id or intent.intent_id),
                    expires_at_ms=_expires_at_ms(intent),
                    maintenance_margin_rate=self._maintenance_margin_rate,
                )
            )
        batch = self._runtime.approve_batch(portfolio=portfolio, intents=tuple(risk_intents))
        if not batch.allowed:
            batch.release()
            raise PermissionError(",".join(batch.reason_codes))
        tokens = {item.fencing_token for item in batch.approvals}
        if tokens != {expected_fencing_token}:
            batch.release()
            raise PermissionError("R5_RISK_FENCING_TOKEN_MISMATCH")
        references = tuple(
            f"{durable_namespace}:{intent.idempotency_key}" for intent in values
        )
        records = batch.confirm(
            commit_port=self._commit_store,
            durable_references=references,
        )
        approvals: dict[str, tuple[OrderIntent, RiskApproval]] = {}
        for intent, decision, record in zip(values, batch.approvals, records, strict=True):
            if record.intent_id != str(intent.intent_id):
                raise RuntimeError("R5 durable risk intent identity mismatch")
            approvals[intent.idempotency_key] = (
                intent,
                RiskApproval(
                    True,
                    decision.approved_quantity,
                    tuple(dict.fromkeys((*decision.reason_codes, "R5_BATCH_DURABLE"))),
                ),
            )
        with self._lock:
            collision = set(approvals).intersection(self._prepared)
            if collision:
                raise RuntimeError("R5 risk batch contains an already-prepared key")
            self._prepared.update(approvals)
        first = batch.approvals[0]
        return R5PreparedRiskBatch(
            tuple(item.idempotency_key for item in values),
            tuple(str(item.decision_id) for item in batch.approvals),
            expected_fencing_token,
            str(first.risk_snapshot_id),
        )

    def approve(self, intent: OrderIntent) -> RiskApproval:
        with self._lock:
            prepared = self._prepared.get(intent.idempotency_key)
        if prepared is None:
            return RiskApproval(False, Decimal("0"), ("R5_BATCH_NOT_PREPARED",))
        original, approval = prepared
        if self._signature(original) != self._signature(intent):
            return RiskApproval(False, Decimal("0"), ("R5_BATCH_INTENT_CHANGED",))
        return approval
