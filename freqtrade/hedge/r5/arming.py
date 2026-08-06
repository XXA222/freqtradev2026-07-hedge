from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable

from .models import R5LimitProfile, R5Stage


PermitValidator = Callable[[str, int], bool]
PermitPreflight = Callable[[str, int, int], int]
PermitConsumer = Callable[[str, int, str], int]


@dataclass(slots=True)
class R5StagePermit:
    stage: R5Stage
    run_id: str
    actor: str
    permit_id: str
    expires_at_monotonic: float
    remaining_writes: int
    authority_epoch: int = 0
    _validator: PermitValidator | None = field(default=None, repr=False, compare=False)
    _preflight_callback: PermitPreflight | None = field(default=None, repr=False, compare=False)
    _consume_callback: PermitConsumer | None = field(default=None, repr=False, compare=False)
    _local_lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    def assert_active(self) -> None:
        self.preflight(0)

    def preflight(self, required_writes: int = 1) -> None:
        if isinstance(required_writes, bool) or not isinstance(required_writes, int):
            raise TypeError("required_writes must be an integer")
        if required_writes < 0:
            raise ValueError("required_writes must not be negative")
        if self._preflight_callback is not None:
            self.remaining_writes = self._preflight_callback(
                self.permit_id, self.authority_epoch, required_writes
            )
            return
        with self._local_lock:
            if self._validator is not None and not self._validator(
                self.permit_id, self.authority_epoch
            ):
                raise PermissionError("R5 stage permit was revoked")
            if time.monotonic() >= self.expires_at_monotonic:
                raise PermissionError("R5 stage permit expired")
            if required_writes > self.remaining_writes:
                raise PermissionError("R5 stage write-request budget exhausted")

    def consume(self, operation: str) -> None:
        name = str(operation).strip().upper()
        if not name or len(name) > 64:
            raise ValueError("R5 write operation name is invalid")
        if self._consume_callback is not None:
            self.remaining_writes = self._consume_callback(
                self.permit_id, self.authority_epoch, name
            )
            return
        with self._local_lock:
            self.preflight(1)
            self.remaining_writes -= 1


class R5StageArmAuthority:
    """Issues revocable, renewable stage permits.

    A permit checks the issuing authority on every use. ``revoke_all`` increments an
    epoch in addition to clearing registrations, which invalidates already-held permit
    objects instead of only preventing future lookup.
    """

    def __init__(self, *, operator_hashes: dict[R5Stage, str], risk_hashes: dict[R5Stage, str]) -> None:
        self._operator = dict(operator_hashes)
        self._risk = dict(risk_hashes)
        self._issued: dict[str, R5StagePermit] = {}
        self._epoch = 1
        self._lock = RLock()

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def _is_active(self, permit_id: str, epoch: int) -> bool:
        with self._lock:
            permit = self._issued.get(str(permit_id))
            return (
                permit is not None
                and epoch == self._epoch
                and permit.authority_epoch == self._epoch
            )

    def _preflight(self, permit_id: str, epoch: int, required_writes: int) -> int:
        with self._lock:
            permit = self._issued.get(str(permit_id))
            if permit is None or epoch != self._epoch or permit.authority_epoch != self._epoch:
                raise PermissionError("R5 stage permit was revoked")
            if time.monotonic() >= permit.expires_at_monotonic:
                raise PermissionError("R5 stage permit expired")
            if required_writes > permit.remaining_writes:
                raise PermissionError("R5 stage write-request budget exhausted")
            return permit.remaining_writes

    def _consume(self, permit_id: str, epoch: int, operation: str) -> int:
        del operation
        with self._lock:
            permit = self._issued.get(str(permit_id))
            if permit is None or epoch != self._epoch or permit.authority_epoch != self._epoch:
                raise PermissionError("R5 stage permit was revoked")
            if time.monotonic() >= permit.expires_at_monotonic:
                raise PermissionError("R5 stage permit expired")
            if permit.remaining_writes < 1:
                raise PermissionError("R5 stage write-request budget exhausted")
            permit.remaining_writes -= 1
            return permit.remaining_writes

    def issue(
        self,
        *,
        stage: R5Stage,
        run_id: str,
        account_fingerprint: str,
        actor: str,
        operator_token: str,
        risk_token: str | None,
        confirmation: str,
        profile: R5LimitProfile,
        ttl_seconds: int = 300,
    ) -> R5StagePermit:
        value = stage if isinstance(stage, R5Stage) else R5Stage(stage)
        if not value.performs_exchange_write:
            raise ValueError("MAINNET_READONLY does not require an R5 write permit")
        if not 30 <= ttl_seconds <= 600:
            raise ValueError("R5 permit TTL must be in [30, 600]")
        actor_text = str(actor).strip()
        run_text = str(run_id).strip()
        fingerprint = str(account_fingerprint).strip()
        if not actor_text or not run_text or not fingerprint:
            raise ValueError("R5 permit identity is incomplete")
        expected_confirmation = f"R5:{value.value}:{run_text}:{fingerprint}"
        if not hmac.compare_digest(str(confirmation), expected_confirmation):
            raise PermissionError("R5 stage confirmation phrase is invalid")
        expected_operator = self._operator.get(value)
        if expected_operator is None or not _matches(operator_token, expected_operator):
            raise PermissionError("R5 operator token is invalid")
        if value.can_create_position:
            expected_risk = self._risk.get(value)
            if expected_risk is None or risk_token is None or not _matches(risk_token, expected_risk):
                raise PermissionError("R5 independent risk token is invalid")
            if hmac.compare_digest(str(operator_token), str(risk_token)):
                raise PermissionError("R5 operator and risk tokens must be different")
        with self._lock:
            epoch = self._epoch
            permit = R5StagePermit(
                value,
                run_text,
                actor_text,
                secrets.token_hex(16),
                time.monotonic() + ttl_seconds,
                profile.write_request_budget,
                authority_epoch=epoch,
                _validator=self._is_active,
                _preflight_callback=self._preflight,
                _consume_callback=self._consume,
            )
            self._issued[permit.permit_id] = permit
        return permit

    def renew(
        self,
        permit: R5StagePermit,
        *,
        ttl_seconds: int = 300,
        additional_writes: int = 0,
        maximum_writes: int | None = None,
    ) -> R5StagePermit:
        """Renew a still-active permit without widening its stage or identity.

        Long-running sessions can refresh short-lived authorization while preserving
        immediate revocation. Write budget may only be added under an explicit maximum.
        """
        if not 30 <= ttl_seconds <= 600:
            raise ValueError("R5 permit TTL must be in [30, 600]")
        if isinstance(additional_writes, bool) or not isinstance(additional_writes, int) or additional_writes < 0:
            raise ValueError("additional_writes must be a nonnegative integer")
        with self._lock:
            current = self._issued.get(permit.permit_id)
            if current is not permit or permit.authority_epoch != self._epoch:
                raise PermissionError("R5 stage permit was revoked")
            if additional_writes:
                if maximum_writes is None or maximum_writes <= 0:
                    raise ValueError("maximum_writes is required when adding write budget")
                permit.remaining_writes = min(
                    permit.remaining_writes + additional_writes,
                    int(maximum_writes),
                )
            permit.expires_at_monotonic = time.monotonic() + ttl_seconds
            return permit

    def revoke(self, permit_id: str) -> bool:
        with self._lock:
            return self._issued.pop(str(permit_id), None) is not None

    def revoke_all(self) -> None:
        with self._lock:
            self._issued.clear()
            self._epoch += 1


def _matches(token: str, expected_hash: str) -> bool:
    actual = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, str(expected_hash).lower())
