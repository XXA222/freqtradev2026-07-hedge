from __future__ import annotations

import json
from pathlib import Path

import pytest

from r30_toolkit.credentials import load_local_binance_credentials
from r30_toolkit.readiness import build_readiness_report, write_report_atomic


def _r24(*, futures=False, ip=False, strict="NOT_REQUESTED"):
    return {
        "status": "FULL_PASS",
        "account_fingerprint": "abc123",
        "clock": {"offset_ms": 222},
        "gate_domains": {"strict_key_policy": strict},
        "checks": [
            {"name": "mode.hedge", "status": "PASS", "details": {}},
            {"name": "margin.cross", "status": "PASS", "details": {}},
            {
                "name": "permission.futures_trading",
                "status": "PASS" if futures else "WARN",
                "details": {"actual": futures},
            },
            {
                "name": "permission.ip_restricted",
                "status": "PASS" if ip else "WARN",
                "details": {"actual": ip},
            },
        ],
        "security": {"exchange_writes": "LOCKED"},
    }


def _r25():
    return {
        "status": "PASS_WITH_NO_USER_EVENTS",
        "scope": {
            "account_fingerprint": "abc123",
            "symbols": ["BTCUSDT", "ETHUSDT"],
        },
        "security": {"exchange_order_writes": "LOCKED"},
    }


def _write(path: Path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_readiness_reports_core_complete_but_live_locked(tmp_path):
    report = build_readiness_report(
        r24_report=_write(tmp_path / "r24.json", _r24()),
        r25_report=_write(tmp_path / "r25.json", _r25()),
    )
    assert report["status"] == "CORE_COMPLETE_LIVE_LOCKED"
    blockers = set(report["live_gate_blockers"])
    assert "FUTURES_TRADING_PERMISSION_REQUIRED" in blockers
    assert "IP_RESTRICTION_REQUIRED" in blockers
    assert "STRICT_KEY_POLICY_REQUIRED" in blockers
    assert "LIVE_TRADING_DISABLED" in blockers
    assert "ARM_TOKEN_NOT_CONFIGURED" in blockers
    assert report["core_checks"]["unknown_order_recovery"] == "PASS"
    assert report["safety"]["cancellation_remains_available_after_disarm"] is True


def test_readiness_rejects_account_mismatch(tmp_path):
    r25 = _r25()
    r25["scope"]["account_fingerprint"] = "other"
    with pytest.raises(ValueError, match="fingerprints"):
        build_readiness_report(
            r24_report=_write(tmp_path / "r24.json", _r24()),
            r25_report=_write(tmp_path / "r25.json", r25),
        )


def test_readiness_hard_filters_to_supported_perpetual_allowlist(tmp_path):
    r25 = _r25()
    r25["scope"]["symbols"] = [
        "BTCUSDT",
        "ETHUSDT",
        "ETHUSDT_261225",
        "SOLUSDT",
        "币安人生USDT",
    ]
    report = build_readiness_report(
        r24_report=_write(tmp_path / "r24.json", _r24()),
        r25_report=_write(tmp_path / "r25.json", r25),
    )
    assert report["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_atomic_report_is_ascii_and_hash_stable(tmp_path):
    payload = {"status": "CORE_COMPLETE_LIVE_LOCKED", "message": "ASCII only"}
    destination = tmp_path / "report.json"
    digest = write_report_atomic(payload, destination)
    raw = destination.read_bytes()
    raw.decode("ascii")
    assert len(digest) == 64
    assert json.loads(raw)["status"] == payload["status"]


def test_local_two_line_credentials_are_redacted(tmp_path):
    source = tmp_path / "binance.txt"
    source.write_text("api-key\napi-secret\n", encoding="utf-8")
    credentials = load_local_binance_credentials(source)
    assert credentials.api_key == "api-key"
    assert credentials.api_secret == "api-secret"
    assert "api-key" not in repr(credentials)
    assert "api-secret" not in repr(credentials)
