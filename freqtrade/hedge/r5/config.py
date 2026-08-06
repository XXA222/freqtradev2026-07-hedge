from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .models import R5LimitProfile, R5Stage, default_limit_profile, exact_decimal


DISABLED_TOKEN_SHA256 = "0" * 64


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def _hash(value: object | None, name: str, *, required: bool) -> str | None:
    if value in (None, "") and not required:
        return None
    text = str(value).strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"{name} must be a SHA256 hex digest")
    return text


def _path(root: Path, value: object | None, default: str) -> Path:
    text = default if value in (None, "") else str(value).strip()
    if not text or "\x00" in text:
        raise ValueError("R5 path is invalid")
    path = Path(text).expanduser()
    return (path if path.is_absolute() else root / path).resolve(strict=False)


def _profile(stage: R5Stage, value: object | None) -> R5LimitProfile:
    base = default_limit_profile(stage)
    if value is None:
        return base
    if not isinstance(value, Mapping):
        raise ValueError(f"profile override for {stage.value} must be an object")
    data = base.to_dict()
    data.update(value)
    data.pop("stage", None)
    return R5LimitProfile(stage=stage, **data)


@dataclass(frozen=True, slots=True)
class R5Config:
    project_root: Path
    credentials_path: Path
    journal_path: Path
    budget_path: Path
    user_stream_report: Path
    r4_report: Path
    r24_report: Path | None
    operator_token_sha256: Mapping[R5Stage, str]
    risk_token_sha256: Mapping[R5Stage, str]
    promotion_token_sha256: str
    profiles: Mapping[R5Stage, R5LimitProfile]
    evidence_max_age_seconds: int = 900
    require_ip_restriction: bool = True
    require_futures_permission: bool = True
    require_zero_unmanaged_positions: bool = True
    require_account_can_trade: bool = True
    require_withdrawals_disabled: bool = True
    require_reading_permission: bool = True
    mainnet_rest_base_url: str = "https://fapi.binance.com"
    sapi_base_url: str = "https://api.binance.com"

    def __post_init__(self) -> None:
        root = self.project_root.expanduser().resolve(strict=False)
        if self.mainnet_rest_base_url != "https://fapi.binance.com":
            raise ValueError("R5 mainnet REST endpoint is fixed to https://fapi.binance.com")
        if self.sapi_base_url != "https://api.binance.com":
            raise ValueError("R5 SAPI endpoint is fixed to https://api.binance.com")
        if isinstance(self.evidence_max_age_seconds, bool) or not isinstance(self.evidence_max_age_seconds, int):
            raise ValueError("evidence_max_age_seconds must be an integer")
        if not 30 <= self.evidence_max_age_seconds <= 3600:
            raise ValueError("evidence_max_age_seconds must be in [30, 3600]")
        for name in (
            "require_ip_restriction",
            "require_futures_permission",
            "require_zero_unmanaged_positions",
            "require_account_can_trade",
            "require_withdrawals_disabled",
            "require_reading_permission",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        operator = dict(self.operator_token_sha256)
        risk = dict(self.risk_token_sha256)
        promotion = _hash(self.promotion_token_sha256, "promotion_token_sha256", required=True)
        for stage in R5Stage:
            if stage.performs_exchange_write and stage not in operator:
                raise ValueError(f"operator token hash is required for {stage.value}")
            if stage.can_create_position and stage not in risk:
                raise ValueError(f"independent risk token hash is required for {stage.value}")
        profiles = {stage: self.profiles.get(stage, default_limit_profile(stage)) for stage in R5Stage}
        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "operator_token_sha256", operator)
        object.__setattr__(self, "risk_token_sha256", risk)
        object.__setattr__(self, "promotion_token_sha256", promotion)
        object.__setattr__(self, "profiles", profiles)


def config_from_mapping(
    values: Mapping[str, Any],
    *,
    project_root: Path,
    allow_unarmed_readonly: bool = False,
) -> R5Config:
    if not isinstance(values, Mapping):
        raise ValueError("R5 configuration must be an object")
    section = values.get("hedge_r5", values)
    if not isinstance(section, Mapping):
        raise ValueError("hedge_r5 must be an object")
    root = project_root.expanduser().resolve(strict=False)
    hashes = section.get("arm_token_sha256", {})
    if not isinstance(hashes, Mapping):
        raise ValueError("arm_token_sha256 must be an object")
    risk_hashes = section.get("risk_token_sha256", {})
    if not isinstance(risk_hashes, Mapping):
        raise ValueError("risk_token_sha256 must be an object")
    def token_hash(mapping: Mapping[str, Any], stage: R5Stage, name: str) -> str:
        raw = mapping.get(stage.value)
        if allow_unarmed_readonly and raw in (None, ""):
            return DISABLED_TOKEN_SHA256
        value = _hash(raw, f"{name}.{stage.value}", required=True)
        assert value is not None
        return value

    operator = {
        stage: token_hash(hashes, stage, "arm_token_sha256")
        for stage in R5Stage if stage.performs_exchange_write
    }
    risk = {
        stage: token_hash(risk_hashes, stage, "risk_token_sha256")
        for stage in R5Stage if stage.can_create_position
    }
    raw_profiles = section.get("profiles", {})
    if not isinstance(raw_profiles, Mapping):
        raise ValueError("profiles must be an object")
    profiles = {stage: _profile(stage, raw_profiles.get(stage.value)) for stage in R5Stage}
    return R5Config(
        project_root=root,
        credentials_path=_path(root, section.get("credentials_path"), "user_data/r5/binance-mainnet-r5.txt"),
        journal_path=_path(root, section.get("journal_path"), "user_data/r5/r5-mainnet-journal.sqlite"),
        budget_path=_path(root, section.get("budget_path"), "user_data/r5/r5-daily-budget.sqlite"),
        user_stream_report=_path(root, section.get("user_stream_report"), "artifacts/r5-inputs/R2.5.3-USER-STREAM-READINESS.json"),
        r4_report=_path(root, section.get("r4_report"), "artifacts/r5-inputs/R4-FULL-COMPLETION-READINESS.json"),
        r24_report=(
            None
            if section.get("r24_report") in (None, "")
            else _path(root, section.get("r24_report"), "")
        ),
        operator_token_sha256=operator,
        risk_token_sha256=risk,
        promotion_token_sha256=(
            DISABLED_TOKEN_SHA256
            if allow_unarmed_readonly and section.get("promotion_token_sha256") in (None, "")
            else _hash(
                section.get("promotion_token_sha256"),
                "promotion_token_sha256",
                required=True,
            )
        ),
        profiles=profiles,
        evidence_max_age_seconds=int(section.get("evidence_max_age_seconds", 900)),
        require_ip_restriction=_bool(section.get("require_ip_restriction", True), "require_ip_restriction"),
        require_futures_permission=_bool(section.get("require_futures_permission", True), "require_futures_permission"),
        require_zero_unmanaged_positions=_bool(section.get("require_zero_unmanaged_positions", True), "require_zero_unmanaged_positions"),
        require_account_can_trade=_bool(section.get("require_account_can_trade", True), "require_account_can_trade"),
        require_withdrawals_disabled=_bool(section.get("require_withdrawals_disabled", True), "require_withdrawals_disabled"),
        require_reading_permission=_bool(section.get("require_reading_permission", True), "require_reading_permission"),
    )


def load_config(
    path: str | Path,
    *,
    project_root: Path | None = None,
    allow_unarmed_readonly: bool = False,
) -> R5Config:
    resolved = Path(path).expanduser().resolve(strict=True)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("R5 configuration root must be an object")
    return config_from_mapping(
        payload,
        project_root=project_root or resolved.parent,
        allow_unarmed_readonly=allow_unarmed_readonly,
    )
