from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .models import R5LimitProfile, R5Stage


@dataclass(frozen=True, slots=True)
class R5PromotionManifest:
    account_id: str
    from_stage: R5Stage
    to_stage: R5Stage
    previous_report_sha256: str
    profile_sha256: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    signature: str

    def __post_init__(self) -> None:
        if self.from_stage is not R5Stage.RESTRICTED_AUTO:
            raise ValueError("R5 promotion must start at RESTRICTED_AUTO")
        if self.to_stage is not R5Stage.EXPANDED_LIMITS:
            raise ValueError("R5 promotion may only target EXPANDED_LIMITS")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("promotion timestamps must be timezone-aware")
        if not self.issued_at < self.expires_at:
            raise ValueError("promotion expiry must follow issue time")
        for value, name in (
            (self.previous_report_sha256, "previous_report_sha256"),
            (self.profile_sha256, "profile_sha256"),
            (self.signature, "signature"),
        ):
            text = str(value).strip().lower()
            if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
                raise ValueError(f"{name} must be a SHA256 hex digest")
        if len(self.nonce) < 24:
            raise ValueError("promotion nonce is too short")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "from_stage": self.from_stage.value,
            "to_stage": self.to_stage.value,
            "previous_report_sha256": self.previous_report_sha256,
            "profile_sha256": self.profile_sha256,
            "nonce": self.nonce,
            "issued_at": self.issued_at.astimezone(UTC).isoformat(),
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "signature": self.signature}


def profile_sha256(profile: R5LimitProfile) -> str:
    raw = json.dumps(
        profile.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def create_promotion_manifest(
    *,
    account_id: str,
    previous_report: Mapping[str, Any],
    profile: R5LimitProfile,
    promotion_token: str,
    ttl_minutes: int = 60,
    now: datetime | None = None,
) -> R5PromotionManifest:
    if profile.stage is not R5Stage.EXPANDED_LIMITS:
        raise ValueError("promotion profile must target EXPANDED_LIMITS")
    if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool) or not 5 <= ttl_minutes <= 1440:
        raise ValueError("ttl_minutes must be in [5, 1440]")
    issued = (now or datetime.now(UTC)).astimezone(UTC)
    previous_bytes = json.dumps(
        previous_report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")
    unsigned = {
        "account_id": account_id,
        "from_stage": R5Stage.RESTRICTED_AUTO.value,
        "to_stage": R5Stage.EXPANDED_LIMITS.value,
        "previous_report_sha256": hashlib.sha256(previous_bytes).hexdigest(),
        "profile_sha256": profile_sha256(profile),
        "nonce": secrets.token_hex(16),
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=ttl_minutes)).isoformat(),
    }
    signature = _sign(unsigned, promotion_token)
    return R5PromotionManifest(
        account_id=account_id,
        from_stage=R5Stage.RESTRICTED_AUTO,
        to_stage=R5Stage.EXPANDED_LIMITS,
        previous_report_sha256=unsigned["previous_report_sha256"],
        profile_sha256=unsigned["profile_sha256"],
        nonce=unsigned["nonce"],
        issued_at=issued,
        expires_at=issued + timedelta(minutes=ttl_minutes),
        signature=signature,
    )


def verify_promotion_manifest(
    manifest: R5PromotionManifest,
    *,
    expected_token_sha256: str,
    promotion_token: str,
    account_id: str,
    previous_report: Mapping[str, Any],
    profile: R5LimitProfile,
    now: datetime | None = None,
) -> None:
    actual_token_hash = hashlib.sha256(promotion_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_token_hash, expected_token_sha256):
        raise PermissionError("R5 promotion token is invalid")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current < manifest.issued_at - timedelta(seconds=30) or current >= manifest.expires_at:
        raise PermissionError("R5 promotion manifest is not currently valid")
    if manifest.account_id != account_id:
        raise PermissionError("R5 promotion account binding mismatch")
    if manifest.profile_sha256 != profile_sha256(profile):
        raise PermissionError("R5 promotion limit profile mismatch")
    previous_bytes = json.dumps(
        previous_report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")
    if manifest.previous_report_sha256 != hashlib.sha256(previous_bytes).hexdigest():
        raise PermissionError("R5 promotion evidence mismatch")
    expected = _sign(manifest.unsigned_payload(), promotion_token)
    if not hmac.compare_digest(expected, manifest.signature):
        raise PermissionError("R5 promotion signature is invalid")


def load_promotion_manifest(path: str | Path) -> R5PromotionManifest:
    value = json.loads(Path(path).expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("promotion manifest must contain an object")
    return R5PromotionManifest(
        account_id=str(value["account_id"]),
        from_stage=R5Stage(value["from_stage"]),
        to_stage=R5Stage(value["to_stage"]),
        previous_report_sha256=str(value["previous_report_sha256"]),
        profile_sha256=str(value["profile_sha256"]),
        nonce=str(value["nonce"]),
        issued_at=datetime.fromisoformat(str(value["issued_at"])),
        expires_at=datetime.fromisoformat(str(value["expires_at"])),
        signature=str(value["signature"]),
    )


def _sign(payload: Mapping[str, Any], token: str) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(token.encode("utf-8"), raw, hashlib.sha256).hexdigest()
