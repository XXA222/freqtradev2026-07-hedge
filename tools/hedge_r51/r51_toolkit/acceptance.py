from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


_STREAM_PASS = {"FULL_PASS", "PASS_WITH_NO_USER_EVENTS"}
_ACCOUNT_BLOCKS = {
    "ACCOUNT_NOT_FLAT",
    "ACTIVE_ORDERS_PRESENT",
    "UNMANAGED_EXPOSURE_PRESENT",
}
_KEY_POLICY_BLOCKS = {
    "READING_PERMISSION_DISABLED",
    "FUTURES_TRADING_PERMISSION_DISABLED",
    "IP_RESTRICTION_DISABLED",
    "WITHDRAWAL_PERMISSION_ENABLED",
    "ACCOUNT_TRADING_DISABLED",
}


def _load(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.expanduser().resolve(strict=True).read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"report must contain a JSON object: {path}")
    return value, raw


def _find_text(value: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        item = value.get(name)
        if isinstance(item, str) and item.strip():
            return item.strip()
    for child in value.values():
        if isinstance(child, Mapping):
            found = _find_text(child, names)
            if found:
                return found
    return ""


def _promotion_next_stage(promotion_ready: bool, blocks: list[str]) -> str:
    if promotion_ready:
        return "ORDER_TEST"
    block_set = set(blocks)
    has_account = bool(block_set & _ACCOUNT_BLOCKS)
    has_key = bool(block_set & _KEY_POLICY_BLOCKS)
    if has_account and has_key:
        return "BLOCKED_KEY_AND_ACCOUNT_STATE"
    if has_key:
        return "BLOCKED_KEY_POLICY"
    if has_account:
        return "BLOCKED_ACCOUNT_STATE"
    return "BLOCKED_PROMOTION_POLICY"


def build_acceptance_report(
    *,
    output: str | Path,
    stage_report: str | Path,
    stream_report: str | Path,
    workspace_manifest: str | Path,
) -> dict[str, Any]:
    stage_path = Path(stage_report)
    stream_path = Path(stream_report)
    manifest_path = Path(workspace_manifest)
    stage, stage_raw = _load(stage_path)
    stream, stream_raw = _load(stream_path)
    manifest, manifest_raw = _load(manifest_path)
    stream_status = _find_text(stream, ("status", "user_stream_status"))
    stage_evidence = stage.get("evidence") if isinstance(stage.get("evidence"), Mapping) else {}
    promotion_ready = bool(stage_evidence.get("order_test_promotion_ready", False))
    promotion_blocks = stage_evidence.get("order_test_promotion_block_reasons", [])
    if not isinstance(promotion_blocks, list) or any(not isinstance(item, str) for item in promotion_blocks):
        promotion_blocks = ["INVALID_PROMOTION_BLOCK_EVIDENCE"]
    promotion_blocks = sorted(set(promotion_blocks))
    stream_fingerprint = _find_text(stream, ("account_fingerprint",))
    expected_r24_fingerprint = str(manifest.get("r24_account_fingerprint", "")).strip()
    stage_r24_fingerprint = str(stage_evidence.get("r24_account_fingerprint", "")).strip()
    readonly_requests = stage_evidence.get("readonly_requests")
    live_r24_fingerprint = str(
        stage_evidence.get("live_evidence_account_fingerprint", "")
    ).strip()
    live_binding_mode = str(stage_evidence.get("live_evidence_binding_mode", "")).strip()
    checks = {
        "mainnet_readonly_stage_pass": stage.get("status") == "PASS",
        "mainnet_readonly_state_completed": stage.get("state") == "COMPLETED",
        "mainnet_readonly_stage_identity": stage.get("stage") == "MAINNET_READONLY",
        "real_mainnet_order_requests_zero": int(stage.get("real_mainnet_order_requests_sent", -1)) == 0,
        "writes_attempted_zero": int(stage.get("writes_attempted", -1)) == 0,
        "fresh_user_stream_pass": stream_status in _STREAM_PASS,
        "workspace_exchange_write_locked": manifest.get("exchange_write") == "LOCKED",
        "workspace_planned_writes_zero": int(manifest.get("real_mainnet_order_requests_planned", -1)) == 0,
        "workspace_credentials_not_persisted": manifest.get("credentials_persisted_by_r51") is False,
        "account_fingerprint_bound": (
            bool(str(manifest.get("account_fingerprint", "")).strip())
            and str(manifest.get("account_fingerprint", "")) in str(stage.get("account_id", ""))
        ),
        "r24_stream_account_bound": (
            bool(expected_r24_fingerprint)
            and expected_r24_fingerprint == stream_fingerprint
            and expected_r24_fingerprint == stage_r24_fingerprint
        ),
        "live_r24_account_bound": (
            bool(expected_r24_fingerprint)
            and expected_r24_fingerprint == live_r24_fingerprint
            and live_binding_mode not in {"", "UNBOUND"}
        ),
        "readonly_requests_observed": isinstance(readonly_requests, int) and readonly_requests > 0,
    }
    failures = sorted(name for name, passed in checks.items() if not passed)
    complete = not failures
    payload: dict[str, Any] = {
        "schema_version": 2,
        "toolkit_version": "5.1.6",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": (
            "R5_1_MAINNET_READONLY_COMPLETE_WRITES_LOCKED"
            if complete
            else "R5_1_MAINNET_READONLY_FAILED"
        ),
        "mainnet_readonly_complete": complete,
        "checks": {name: "PASS" if passed else "FAIL" for name, passed in sorted(checks.items())},
        "required_failures": failures,
        "evidence": {
            "stage_report": str(stage_path.resolve()),
            "stage_report_sha256": hashlib.sha256(stage_raw).hexdigest(),
            "stream_report": str(stream_path.resolve()),
            "stream_report_sha256": hashlib.sha256(stream_raw).hexdigest(),
            "workspace_manifest": str(manifest_path.resolve()),
            "workspace_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "stream_status": stream_status,
            "account_id": stage.get("account_id"),
            "readonly_requests": readonly_requests,
            "r24_account_fingerprint": expected_r24_fingerprint,
            "live_evidence_account_fingerprint": live_r24_fingerprint,
            "live_evidence_binding_mode": live_binding_mode,
            "order_test_promotion_ready": promotion_ready,
            "order_test_promotion_block_reasons": promotion_blocks,
            "promotion_block_categories": {
                "account_state": sorted(set(promotion_blocks) & _ACCOUNT_BLOCKS),
                "key_policy": sorted(set(promotion_blocks) & _KEY_POLICY_BLOCKS),
                "other": sorted(set(promotion_blocks) - _ACCOUNT_BLOCKS - _KEY_POLICY_BLOCKS),
            },
        },
        "mainnet_live_exchange_write": "LOCKED",
        "real_mainnet_order_requests_sent": 0,
        "next_stage": (
            _promotion_next_stage(promotion_ready, promotion_blocks)
            if complete
            else "BLOCKED_VALIDATION_FAILURE"
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    out = Path(output).expanduser().resolve(strict=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="ascii")
    temporary.replace(out)
    return payload
