"""Bound, fresh user-stream evidence for R5.3.1 authoritative writes.

The write composition never infers WebSocket health from REST timestamps or defaults.
It consumes the persisted R5.1 user-stream report through the existing evidence loader,
which validates freshness, write-lock retention, account binding, reconciliation and
stream health before any production gate can be armed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .evidence import load_evidence_bundle


@dataclass(frozen=True, slots=True)
class R5UserStreamEvidence:
    status: str
    observed_at: datetime
    account_fingerprint: str
    report_path: Path
    report_sha256: str
    stream_connected: bool
    stream_fresh: bool
    gap_detected: bool
    reconciliation_converged: bool

    @classmethod
    def load(
        cls,
        *,
        r4_report: str | Path,
        user_stream_report: str | Path,
        r24_report: str | Path,
        max_age_seconds: int,
    ) -> "R5UserStreamEvidence":
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
            raise TypeError("max_age_seconds must be an integer")
        if not 30 <= max_age_seconds <= 3600:
            raise ValueError("max_age_seconds must be in [30, 3600]")
        bundle = load_evidence_bundle(
            r4_report=Path(r4_report),
            user_stream_report=Path(user_stream_report),
            r24_report=Path(r24_report),
            max_age_seconds=max_age_seconds,
        )
        payload = json.loads(bundle.user_stream_report_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, Mapping):
            raise ValueError("R5 user-stream evidence must be a JSON object")
        health = payload.get("direction2_health")
        if not isinstance(health, Mapping):
            raise PermissionError("R5 user-stream evidence has no direction2_health block")
        reason_codes = health.get("reason_codes", ())
        normalized_reasons = {
            str(item).strip().upper()
            for item in (reason_codes if isinstance(reason_codes, (list, tuple)) else ())
            if str(item).strip()
        }
        gap_detected = bool(
            health.get("gap_detected", False)
            or payload.get("gap_detected", False)
            or any("GAP" in item or "SEQUENCE" in item for item in normalized_reasons)
        )
        stream_connected = health.get("stream_connected") is True
        stream_fresh = health.get("stream_fresh") is True
        reconciliation = health.get("reconciliation_consistent") is True
        if not stream_connected or not stream_fresh or not reconciliation or gap_detected:
            raise PermissionError("R5 user-stream evidence is not write-ready")
        return cls(
            status=bundle.user_stream_status,
            observed_at=bundle.user_stream_created_at,
            account_fingerprint=bundle.stream_account_fingerprint,
            report_path=bundle.user_stream_report_path,
            report_sha256=bundle.user_stream_sha256,
            stream_connected=stream_connected,
            stream_fresh=stream_fresh,
            gap_detected=gap_detected,
            reconciliation_converged=reconciliation,
        )
