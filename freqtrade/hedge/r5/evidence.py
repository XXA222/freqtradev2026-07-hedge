from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from freqtrade.hedge.execution.binance_environment import ExecutionEnvironment
from freqtrade.hedge.execution.production_gate import ProductionGateEvidence

from .models import R5LimitProfile, R5_ALLOWED_SYMBOLS


_R4_PASS = {"R4_FULL_COMPLETION_COMPLETE_MAINNET_LOCKED"}
_R24_PASS = {"FULL_PASS", "PASS_WITH_DUAL_LEG_OBSERVATION_GAP"}
_STREAM_PASS = {"FULL_PASS", "PASS_WITH_NO_USER_EVENTS"}


@dataclass(frozen=True, slots=True)
class R5EvidenceBundle:
    # Keep the original R5.0 positional constructor stable. New R5.1 account-binding
    # fields are additive and are populated by load_evidence_bundle.
    r4_report_path: Path
    user_stream_report_path: Path
    r4_sha256: str
    user_stream_sha256: str
    user_stream_status: str
    user_stream_created_at: datetime
    r24_report_path: Path | None = None
    r24_sha256: str = ""
    stream_account_fingerprint: str = ""
    r24_account_fingerprint: str = ""


def load_evidence_bundle(
    *,
    r4_report: Path,
    user_stream_report: Path,
    max_age_seconds: int,
    r24_report: Path | None = None,
) -> R5EvidenceBundle:
    r4_path = r4_report.expanduser().resolve(strict=True)
    stream_path = user_stream_report.expanduser().resolve(strict=True)
    r4_raw = r4_path.read_bytes()
    stream_raw = stream_path.read_bytes()
    r4 = json.loads(r4_raw)
    stream = json.loads(stream_raw)
    if not isinstance(r4, Mapping) or not isinstance(stream, Mapping):
        raise ValueError("R5 evidence reports must contain JSON objects")
    if str(r4.get("status", "")) not in _R4_PASS:
        raise PermissionError("R5 requires a passing R4 full-completion report")
    if str(r4.get("mainnet_live_exchange_write", "LOCKED")) != "LOCKED":
        raise PermissionError("R5 prerequisite R4 report must remain mainnet locked")
    if int(r4.get("real_mainnet_order_requests_sent", 0)) != 0:
        raise PermissionError("R5 prerequisite R4 report contains mainnet writes")

    status = _find_text(stream, ("status", "user_stream_status"))
    if status not in _STREAM_PASS:
        raise PermissionError("R5 requires a passing mainnet user-stream report")
    created = _find_datetime(stream)
    age = (datetime.now(UTC) - created).total_seconds()
    if age < -30 or age > max_age_seconds:
        raise PermissionError("R5 user-stream evidence is stale or from the future")
    _assert_stream_health(stream)

    stream_fingerprint = _nested_text(stream, "scope", "account_fingerprint")
    if not stream_fingerprint:
        raise PermissionError("R5 user-stream report does not bind an account fingerprint")

    r24_path = _resolve_r24_path(r24_report, stream)
    r24_raw = r24_path.read_bytes()
    r24 = json.loads(r24_raw)
    if not isinstance(r24, Mapping):
        raise ValueError("R2.4.1 evidence report must contain a JSON object")
    if str(r24.get("status", "")) not in _R24_PASS:
        raise PermissionError("R5 requires a passing R2.4.1 authenticated-readonly report")
    security = r24.get("security")
    if not isinstance(security, Mapping) or security.get("exchange_writes") != "LOCKED":
        raise PermissionError("R2.4.1 evidence must retain exchange write lock")
    r24_fingerprint = str(r24.get("account_fingerprint", "")).strip()
    if not r24_fingerprint:
        raise PermissionError("R2.4.1 evidence does not contain account_fingerprint")
    if r24_fingerprint != stream_fingerprint:
        raise PermissionError("R2.4.1 and R2.5.x evidence refer to different accounts")

    return R5EvidenceBundle(
        r4_report_path=r4_path,
        user_stream_report_path=stream_path,
        r24_report_path=r24_path,
        r4_sha256=hashlib.sha256(r4_raw).hexdigest(),
        user_stream_sha256=hashlib.sha256(stream_raw).hexdigest(),
        r24_sha256=hashlib.sha256(r24_raw).hexdigest(),
        user_stream_status=status,
        user_stream_created_at=created,
        stream_account_fingerprint=stream_fingerprint,
        r24_account_fingerprint=r24_fingerprint,
    )


def build_production_gate_evidence(
    *,
    account_fingerprint: str,
    profile: R5LimitProfile,
    readonly_status: str,
    user_stream_status: str,
    hedge_mode_enabled: bool,
    clock_offset_ms: int,
    futures_trading_permission: bool,
    ip_restricted: bool,
    cross_margin_symbols: tuple[str, ...],
    expected_arm_token_sha256: str,
) -> ProductionGateEvidence:
    return ProductionGateEvidence(
        environment=ExecutionEnvironment.LIVE,
        account_fingerprint=account_fingerprint,
        allowed_symbols=R5_ALLOWED_SYMBOLS,
        cross_margin_symbols=cross_margin_symbols,
        readonly_status=readonly_status,
        user_stream_status=user_stream_status,
        hedge_mode_enabled=hedge_mode_enabled,
        clock_offset_ms=clock_offset_ms,
        live_trading_enabled=True,
        strict_key_policy_passed=futures_trading_permission and ip_restricted,
        futures_trading_permission=futures_trading_permission,
        ip_restricted=ip_restricted,
        expected_arm_token_sha256=expected_arm_token_sha256,
        max_abs_clock_offset_ms=1000,
        max_order_notional=profile.max_single_order_notional,
        allow_market_orders=False,
        account_id_prefix="binance-usdm",
    )


def _resolve_r24_path(explicit: Path | None, stream: Mapping[str, Any]) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve(strict=True)
    prerequisite = stream.get("r24_prerequisite")
    if isinstance(prerequisite, Mapping):
        raw = prerequisite.get("report")
        if isinstance(raw, str) and raw.strip():
            return Path(raw.strip()).expanduser().resolve(strict=True)
    raise PermissionError("R5 configuration must bind the R2.4.1 prerequisite report")


def _assert_stream_health(stream: Mapping[str, Any]) -> None:
    result = stream.get("result")
    health = stream.get("direction2_health")
    security = stream.get("security")
    database = stream.get("database")
    scope = stream.get("scope")
    if not isinstance(result, Mapping) or result.get("final_state") != "READY" or result.get("ready_observed") is not True:
        raise PermissionError("R5 user-stream evidence did not reach READY")
    if not isinstance(health, Mapping):
        raise PermissionError("R5 user-stream evidence does not contain health facts")
    for name in ("clock_synchronized", "configuration_valid", "reconciliation_consistent", "rest_fresh", "stream_connected", "stream_fresh"):
        if health.get(name) is not True:
            raise PermissionError(f"R5 user-stream health check failed: {name}")
    if int(health.get("unmanaged_order_count", 0)) != 0 or int(health.get("unmanaged_position_count", 0)) != 0:
        raise PermissionError("R5 user-stream evidence contains unmanaged exposure")
    if not isinstance(security, Mapping):
        raise PermissionError("R5 user-stream evidence does not contain security facts")
    if security.get("exchange_order_writes") != "LOCKED":
        raise PermissionError("R5 user-stream evidence did not retain exchange write lock")
    if security.get("trading_endpoints_allowed") is not False:
        raise PermissionError("R5 user-stream evidence allowed trading endpoints")
    if security.get("credentials_persisted") is not False:
        raise PermissionError("R5 user-stream evidence persisted credentials")
    write_lock = security.get("write_lock")
    if not isinstance(write_lock, Mapping) or write_lock.get("enabled") is not True:
        raise PermissionError("R5 user-stream evidence does not prove write-lock activation")
    if not isinstance(database, Mapping):
        raise PermissionError("R5 user-stream evidence does not contain database integrity")
    if database.get("reopen_readback") != "PASS":
        raise PermissionError("R5 user-stream database reopen check failed")
    for name in ("integrity_before_reopen", "integrity_after_reopen"):
        value = database.get(name)
        if not isinstance(value, Mapping) or value.get("status") != "PASS":
            raise PermissionError(f"R5 user-stream database check failed: {name}")
    if not isinstance(scope, Mapping):
        raise PermissionError("R5 user-stream evidence does not contain symbol scope")
    symbols = scope.get("symbols")
    if not isinstance(symbols, list) or tuple(symbols) != R5_ALLOWED_SYMBOLS:
        raise PermissionError("R5 user-stream evidence symbol scope is not BTCUSDT/ETHUSDT")


def _nested_text(value: Mapping[str, Any], parent: str, name: str) -> str:
    child = value.get(parent)
    if not isinstance(child, Mapping):
        return ""
    raw = child.get(name)
    return raw.strip() if isinstance(raw, str) else ""


def _find_text(value: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for child in value.values():
        if isinstance(child, Mapping):
            found = _find_text(child, names)
            if found:
                return found
    return ""


_EVIDENCE_TIMESTAMP_FIELDS = (
    "completed_at_utc",
    "created_at_utc",
    "generated_at_utc",
    "started_at_utc",
    "completed_at",
    "created_at",
    "generated_at",
    "started_at",
)


def _find_datetime(value: Mapping[str, Any]) -> datetime:
    for name in _EVIDENCE_TIMESTAMP_FIELDS:
        raw = value.get(name)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            result = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if result.tzinfo is not None and result.utcoffset() is not None:
            return result.astimezone(UTC)
    for child in value.values():
        if isinstance(child, Mapping):
            try:
                return _find_datetime(child)
            except ValueError:
                continue
    raise ValueError(
        "user-stream report does not contain a recognized timezone-aware evidence timestamp"
    )
