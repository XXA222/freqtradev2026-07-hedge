from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import TOOLKIT_VERSION


def _text(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def _contains(root: Path, relative: str, *tokens: str) -> bool:
    path = root / relative
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return all(token in text for token in tokens)


def run(args: argparse.Namespace) -> int:
    root = Path(args.project_root).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().resolve(strict=False)
    checks: dict[str, bool] = {
        "real_user_stream_evidence_required": _contains(
            root,
            "freqtrade/hedge/r5/authoritative_mainnet.py",
            "R5UserStreamEvidence.load",
            "r4_report",
            "r24_report",
            "user_stream_report",
        ),
        "user_stream_defaults_removed": 'user_stream_status: str = "FULL_PASS"' not in _text(
            root, "freqtrade/hedge/r5/authoritative_mainnet.py"
        ),
        "unresolved_recovery_blocks": _contains(
            root,
            "freqtrade/hedge/r5/authoritative_mainnet.py",
            "if recovery.unresolved:",
            "R5 recovery contains unresolved orders",
        ),
        "portfolio_refreshed_after_recovery": _contains(
            root,
            "freqtrade/hedge/r5/authoritative_mainnet.py",
            "refreshed_preflight = read_api.preflight()",
            "refreshed_portfolio = build_live_risk_portfolio",
        ),
        "dynamic_readiness_gate": _contains(
            root,
            "freqtrade/hedge/r5/authoritative_runtime.py",
            "class _RiskRuntimeReadinessGate",
            "self.runtime.refresh()",
        ),
        "always_ready_removed": "AlwaysReadyGate" not in _text(
            root, "freqtrade/hedge/r5/authoritative_runtime.py"
        ),
        "lease_runner_enabled": _contains(
            root,
            "freqtrade/hedge/r5/authoritative_mainnet.py",
            "enable_lease_runner=True",
        ),
        "runtime_budget_mandatory": _contains(
            root,
            "freqtrade/hedge/r5/authoritative_runtime.py",
            "budget: R5BudgetPort",
            "R5 budget reservation coverage is incomplete",
        ),
        "fill_does_not_write_daily_loss": "record_realized_pnl" not in _text(
            root, "freqtrade/hedge/r5/fill_closed_loop.py"
        ),
        "session_does_not_write_daily_loss": "record_realized_pnl" not in _text(
            root, "freqtrade/hedge/r5/production_session.py"
        ),
        "stage_permit_atomic_callback": _contains(
            root,
            "freqtrade/hedge/r5/arming.py",
            "_consume_callback",
            "def _consume(",
            "with self._lock:",
        ),
        "emergency_permit_atomic_callback": _contains(
            root,
            "freqtrade/hedge/r5/emergency.py",
            "_consumer",
            "self._issued.pop",
        ),
        "cursor_completed_after_post_validation": (
            _text(root, "freqtrade/hedge/r5/production_session.py").find(
                "self._check_snapshot(self.snapshot_provider())"
            )
            < _text(root, "freqtrade/hedge/r5/production_session.py").find(
                "self.store.complete_decision_cursor"
            )
        ),
        "security_scanner_no_literal_self_trigger": "secret_tokens =" not in _text(
            root, "tools/hedge_r53/r53_toolkit/diagnostics.py"
        ),
        "offline_acceptance_disposes_engine": _contains(
            root,
            "tools/hedge_r53/r53_toolkit/offline_acceptance.py",
            "cursor.close()",
            "engine.dispose()",
        ),
        "r531_tests_present": (root / "tools/hedge_r531/tests/test_r531_safety_closure.py").is_file(),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "R5_3_1_SAFETY_CLOSURE_PASS" if not failed else "R5_3_1_SAFETY_CLOSURE_FAIL",
        "check_count": len(checks),
        "passed_count": len(checks) - len(failed),
        "failed_count": len(failed),
        "failed_checks": failed,
        "checks": checks,
        "exchange_access": "NONE",
        "real_mainnet_order_requests_sent": 0,
        "mainnet_writes": "LOCKED_PENDING_REAL_EVIDENCE_AND_24_72H_SOAK",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="ascii")
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if not failed else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r531-offline-acceptance")
    value.add_argument("--project-root", required=True)
    value.add_argument("--output", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
