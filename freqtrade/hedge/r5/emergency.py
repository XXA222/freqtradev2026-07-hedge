"""Dedicated, revocable, dual-authorized emergency reduce-only MARKET exit."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Callable

from freqtrade.hedge.execution.service import OrderIntent, OrderType, PositionSide, IntentAction


@dataclass(slots=True)
class R5EmergencyExitPermit:
    permit_id: str
    account_id: str
    symbol: str
    position_side: PositionSide
    max_quantity: Decimal
    max_notional: Decimal
    reason: str
    expires_at_monotonic: float
    authority_epoch: int
    _validator: Callable[[str, int], bool]
    _consumer: Callable[[str, int], bool] | None = None
    consumed: bool = False

    def assert_active(self) -> None:
        if self.consumed:
            raise PermissionError("emergency permit already consumed")
        if not self._validator(self.permit_id, self.authority_epoch):
            raise PermissionError("emergency permit was revoked")
        if time.monotonic() >= self.expires_at_monotonic:
            raise PermissionError("emergency permit expired")

    def authorize(self, intent: OrderIntent) -> None:
        self.assert_active()
        if intent.account_id != self.account_id or intent.symbol != self.symbol:
            raise PermissionError("emergency permit scope mismatch")
        if intent.position_side is not self.position_side:
            raise PermissionError("emergency permit position-side mismatch")
        if intent.order_type is not OrderType.MARKET or not intent.reduce_only:
            raise PermissionError("emergency permit allows reduce-only MARKET only")
        if intent.action not in {IntentAction.REDUCE, IntentAction.CLOSE}:
            raise PermissionError("emergency permit action must reduce risk")
        reference = Decimal(str(intent.metadata.get("reference_price", "0")))
        if reference <= 0:
            raise PermissionError("emergency reference price is required")
        if intent.quantity > self.max_quantity or intent.quantity * reference > self.max_notional:
            raise PermissionError("emergency permit quantity/notional exceeded")
        if self._consumer is not None:
            if not self._consumer(self.permit_id, self.authority_epoch):
                raise PermissionError("emergency permit already consumed or revoked")
        elif self.consumed:
            raise PermissionError("emergency permit already consumed")
        self.consumed = True


class R5EmergencyExitAuthority:
    def __init__(self, *, operator_sha256: str, risk_sha256: str) -> None:
        self._operator = str(operator_sha256).lower()
        self._risk = str(risk_sha256).lower()
        if len(self._operator) != 64 or len(self._risk) != 64 or self._operator == self._risk:
            raise ValueError("independent emergency token hashes are required")
        self._issued: dict[str, R5EmergencyExitPermit] = {}
        self._epoch = 1
        self._lock = RLock()

    @staticmethod
    def _matches(token: str, expected: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(str(token).encode()).hexdigest(), expected)

    def _active(self, permit_id: str, epoch: int) -> bool:
        with self._lock:
            return epoch == self._epoch and permit_id in self._issued

    def _consume(self, permit_id: str, epoch: int) -> bool:
        with self._lock:
            permit = self._issued.get(str(permit_id))
            if permit is None or epoch != self._epoch or permit.authority_epoch != self._epoch:
                return False
            if permit.consumed or time.monotonic() >= permit.expires_at_monotonic:
                return False
            permit.consumed = True
            self._issued.pop(str(permit_id), None)
            return True

    def issue(
        self,
        *,
        account_id: str,
        symbol: str,
        position_side: PositionSide,
        max_quantity: Decimal,
        max_notional: Decimal,
        reason: str,
        operator_token: str,
        risk_token: str,
        confirmation: str,
        ttl_seconds: int = 90,
    ) -> R5EmergencyExitPermit:
        if not 30 <= ttl_seconds <= 120:
            raise ValueError("emergency TTL must be in [30, 120]")
        if not self._matches(operator_token, self._operator) or not self._matches(risk_token, self._risk):
            raise PermissionError("emergency dual authorization failed")
        if hmac.compare_digest(str(operator_token), str(risk_token)):
            raise PermissionError("emergency tokens must be independent")
        expected = f"EMERGENCY_CLOSE:{account_id}:{symbol}:{position_side.value}:{max_quantity}"
        if not hmac.compare_digest(str(confirmation), expected):
            raise PermissionError("emergency confirmation phrase is invalid")
        if max_quantity <= 0 or max_notional <= 0:
            raise ValueError("emergency limits must be positive")
        permit_id = secrets.token_hex(16)
        with self._lock:
            permit = R5EmergencyExitPermit(
                permit_id,
                account_id,
                symbol,
                position_side,
                max_quantity,
                max_notional,
                str(reason).strip(),
                time.monotonic() + ttl_seconds,
                self._epoch,
                self._active,
                self._consume,
            )
            self._issued[permit_id] = permit
            return permit

    def revoke_all(self) -> None:
        with self._lock:
            self._issued.clear()
            self._epoch += 1
