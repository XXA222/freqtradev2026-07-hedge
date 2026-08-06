from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .strategy_runtime import R5StrategyFacts
from .strategy_store import R5StrategyStateStore


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _signature(token: str, payload: bytes) -> str:
    text = str(token)
    if len(text) < 16:
        raise ValueError("R5.2 adoption token must contain at least 16 characters")
    return hmac.new(text.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class R5PositionAdoptionManifest:
    account_id: str
    account_fingerprint: str
    symbol: str
    long_quantity: Decimal
    long_average_price: Decimal
    short_quantity: Decimal
    short_average_price: Decimal
    external_order_count: int
    created_at: datetime
    expires_at: datetime
    nonce: str
    payload_sha256: str
    operator_signature: str
    risk_signature: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "account_id": self.account_id,
            "account_fingerprint": self.account_fingerprint,
            "symbol": self.symbol,
            "long_quantity": str(self.long_quantity),
            "long_average_price": str(self.long_average_price),
            "short_quantity": str(self.short_quantity),
            "short_average_price": str(self.short_average_price),
            "external_order_count": self.external_order_count,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "nonce": self.nonce,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.payload(),
            "payload_sha256": self.payload_sha256,
            "operator_signature": self.operator_signature,
            "risk_signature": self.risk_signature,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "R5PositionAdoptionManifest":
        return cls(
            account_id=str(value["account_id"]),
            account_fingerprint=str(value["account_fingerprint"]),
            symbol=str(value["symbol"]),
            long_quantity=Decimal(str(value["long_quantity"])),
            long_average_price=Decimal(str(value["long_average_price"])),
            short_quantity=Decimal(str(value["short_quantity"])),
            short_average_price=Decimal(str(value["short_average_price"])),
            external_order_count=int(value["external_order_count"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            expires_at=datetime.fromisoformat(str(value["expires_at"])),
            nonce=str(value["nonce"]),
            payload_sha256=str(value["payload_sha256"]),
            operator_signature=str(value["operator_signature"]),
            risk_signature=str(value["risk_signature"]),
        )


def build_position_adoption_manifest(
    *,
    facts: R5StrategyFacts,
    account_fingerprint: str,
    operator_token: str,
    risk_token: str,
    ttl_seconds: int = 300,
    now: datetime | None = None,
) -> R5PositionAdoptionManifest:
    if not 30 <= ttl_seconds <= 900:
        raise ValueError("R5.2 adoption TTL must be in [30, 900] seconds")
    created = now or datetime.now(UTC)
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("R5.2 adoption time must be timezone-aware")
    fingerprint = str(account_fingerprint).strip()
    if len(fingerprint) < 16:
        raise ValueError("R5.2 account fingerprint is invalid")
    payload = {
        "schema_version": 1,
        "account_id": facts.account_id,
        "account_fingerprint": fingerprint,
        "symbol": facts.symbol,
        "long_quantity": str(facts.long_quantity),
        "long_average_price": str(facts.long_average_price),
        "short_quantity": str(facts.short_quantity),
        "short_average_price": str(facts.short_average_price),
        "external_order_count": facts.external_order_count,
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(seconds=ttl_seconds)).isoformat(),
        "nonce": secrets.token_hex(16),
    }
    canonical = _canonical(payload)
    digest = hashlib.sha256(canonical).hexdigest()
    return R5PositionAdoptionManifest(
        account_id=facts.account_id,
        account_fingerprint=fingerprint,
        symbol=facts.symbol,
        long_quantity=facts.long_quantity,
        long_average_price=facts.long_average_price,
        short_quantity=facts.short_quantity,
        short_average_price=facts.short_average_price,
        external_order_count=facts.external_order_count,
        created_at=created,
        expires_at=created + timedelta(seconds=ttl_seconds),
        nonce=str(payload["nonce"]),
        payload_sha256=digest,
        operator_signature=_signature(operator_token, canonical),
        risk_signature=_signature(risk_token, canonical),
    )


def verify_position_adoption_manifest(
    manifest: R5PositionAdoptionManifest,
    *,
    facts: R5StrategyFacts,
    expected_account_fingerprint: str,
    operator_token: str,
    risk_token: str,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("R5.2 verification time must be timezone-aware")
    if manifest.created_at.tzinfo is None or manifest.expires_at.tzinfo is None:
        raise ValueError("R5.2 adoption manifest timestamps must be timezone-aware")
    if current < manifest.created_at - timedelta(seconds=2) or current > manifest.expires_at:
        raise PermissionError("R5.2 adoption manifest is not currently valid")
    if manifest.expires_at - manifest.created_at > timedelta(minutes=15):
        raise PermissionError("R5.2 adoption manifest TTL exceeds the hard limit")
    expected = {
        "account_id": facts.account_id,
        "account_fingerprint": str(expected_account_fingerprint).strip(),
        "symbol": facts.symbol,
        "long_quantity": facts.long_quantity,
        "long_average_price": facts.long_average_price,
        "short_quantity": facts.short_quantity,
        "short_average_price": facts.short_average_price,
        "external_order_count": facts.external_order_count,
    }
    actual = {
        "account_id": manifest.account_id,
        "account_fingerprint": manifest.account_fingerprint,
        "symbol": manifest.symbol,
        "long_quantity": manifest.long_quantity,
        "long_average_price": manifest.long_average_price,
        "short_quantity": manifest.short_quantity,
        "short_average_price": manifest.short_average_price,
        "external_order_count": manifest.external_order_count,
    }
    if actual != expected:
        raise PermissionError("R5.2 adoption manifest no longer matches live facts")
    canonical = _canonical(manifest.payload())
    digest = hashlib.sha256(canonical).hexdigest()
    if not hmac.compare_digest(digest, manifest.payload_sha256):
        raise PermissionError("R5.2 adoption manifest payload hash mismatch")
    if not hmac.compare_digest(_signature(operator_token, canonical), manifest.operator_signature):
        raise PermissionError("R5.2 adoption operator signature mismatch")
    if not hmac.compare_digest(_signature(risk_token, canonical), manifest.risk_signature):
        raise PermissionError("R5.2 adoption risk signature mismatch")
    return digest


def apply_position_adoption_manifest(
    *,
    store: R5StrategyStateStore,
    manifest: R5PositionAdoptionManifest,
    facts: R5StrategyFacts,
    expected_account_fingerprint: str,
    operator_token: str,
    risk_token: str,
    now: datetime | None = None,
) -> str:
    digest = verify_position_adoption_manifest(
        manifest,
        facts=facts,
        expected_account_fingerprint=expected_account_fingerprint,
        operator_token=operator_token,
        risk_token=risk_token,
        now=now,
    )
    store.adopt_core_baseline(
        account_id=facts.account_id,
        symbol=facts.symbol,
        long_quantity=facts.long_quantity,
        long_average_price=facts.long_average_price,
        short_quantity=facts.short_quantity,
        short_average_price=facts.short_average_price,
        manifest_sha256=digest,
    )
    return digest
