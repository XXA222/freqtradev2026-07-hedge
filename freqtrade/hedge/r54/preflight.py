from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class MainnetReadonlyPreflightResult:
    private_api_ok: bool
    hedge_mode: bool
    cross_symbols: tuple[str, ...]
    required_cross_symbols: tuple[str, ...]
    clock_offset_ms: int
    max_abs_clock_offset_ms: int
    user_stream_connected: bool
    user_stream_fresh: bool
    gap_detected: bool
    reconciliation_diff_count: int
    unknown_order_count: int
    database_persistent: bool
    restart_replay_ok: bool
    exchange_writes_locked: bool
    account_fingerprint: str

    def __post_init__(self) -> None:
        if not self.account_fingerprint.strip():
            raise ValueError("account_fingerprint is required")
        if self.max_abs_clock_offset_ms <= 0:
            raise ValueError("max_abs_clock_offset_ms must be positive")
        if self.reconciliation_diff_count < 0 or self.unknown_order_count < 0:
            raise ValueError("counts must be nonnegative")

    @property
    def passed(self) -> bool:
        return (
            self.private_api_ok
            and self.hedge_mode
            and set(self.required_cross_symbols).issubset(self.cross_symbols)
            and abs(self.clock_offset_ms) <= self.max_abs_clock_offset_ms
            and self.user_stream_connected
            and self.user_stream_fresh
            and not self.gap_detected
            and self.reconciliation_diff_count == 0
            and self.unknown_order_count == 0
            and self.database_persistent
            and self.restart_replay_ok
            and self.exchange_writes_locked
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "private_api_ok": self.private_api_ok,
            "hedge_mode": self.hedge_mode,
            "cross_symbols": list(self.cross_symbols),
            "required_cross_symbols": list(self.required_cross_symbols),
            "clock_offset_ms": self.clock_offset_ms,
            "max_abs_clock_offset_ms": self.max_abs_clock_offset_ms,
            "user_stream_connected": self.user_stream_connected,
            "user_stream_fresh": self.user_stream_fresh,
            "gap_detected": self.gap_detected,
            "reconciliation_diff_count": self.reconciliation_diff_count,
            "unknown_order_count": self.unknown_order_count,
            "database_persistent": self.database_persistent,
            "restart_replay_ok": self.restart_replay_ok,
            "exchange_writes_locked": self.exchange_writes_locked,
            "account_fingerprint": self.account_fingerprint,
        }


def build_mainnet_preflight_evidence(
    result: MainnetReadonlyPreflightResult,
    *,
    account_id: str,
    producer: str = "r54-mainnet-readonly-preflight",
    ttl_seconds: int = 900,
) -> EvidenceEnvelope:
    return create_envelope(
        kind=ArtifactKind.MAINNET_READONLY_PREFLIGHT,
        account_id=account_id,
        environment="MAINNET",
        producer=producer,
        payload=result.to_payload(),
        status=CheckStatus.PASS if result.passed else CheckStatus.FAIL,
        ttl_seconds=ttl_seconds,
        write_surface="NONE",
        real_order_requests=0,
    )
