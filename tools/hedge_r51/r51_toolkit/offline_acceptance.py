from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_FORBIDDEN_READONLY_TOKENS = (
    '"POST", "/fapi/v1/order"',
    '"DELETE", "/fapi/v1/order"',
    '"PUT", "/fapi/v1/order"',
    "/fapi/v1/order/test",
)


def build_report(*, project_root: str | Path, output: str | Path) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve(strict=True)
    required = {
        "read_api": root / "freqtrade/hedge/r5/mainnet_api.py",
        "bootstrap": root / "freqtrade/hedge/r5/readonly_bootstrap.py",
        "credentials": root / "freqtrade/hedge/r5/credentials.py",
        "evidence": root / "freqtrade/hedge/r5/evidence.py",
        "journal": root / "freqtrade/hedge/r5/journal.py",
        "orchestrator": root / "freqtrade/hedge/r5/orchestrator.py",
        "acceptance": root / "tools/hedge_r51/r51_toolkit/acceptance.py",
        "runner": root / "scripts/Run-Freqtrade-Hedge-R5.1-MainnetReadonly.ps1",
        "transport": root / "freqtrade/hedge/execution/binance_usdm_adapter.py",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    texts = {
        name: path.read_text(encoding="ascii" if path.suffix.lower() == ".ps1" else "utf-8")
        for name, path in required.items() if path.is_file()
    }
    combined_readonly = "\n".join(texts.get(name, "") for name in ("read_api", "bootstrap", "runner"))
    forbidden = sorted(token for token in _FORBIDDEN_READONLY_TOKENS if token in combined_readonly)
    runner = texts.get("runner", "")
    bootstrap = texts.get("bootstrap", "")
    evidence = texts.get("evidence", "")
    journal = texts.get("journal", "")
    orchestrator = texts.get("orchestrator", "")
    acceptance = texts.get("acceptance", "")
    read_api = texts.get("read_api", "")
    transport = texts.get("transport", "")
    checks = {
        "required_files_present": not missing,
        "readonly_surface_has_no_order_endpoints": not forbidden,
        "runner_forces_mainnet_readonly": '"--stage", "MAINNET_READONLY"' in runner,
        "runner_sets_exchange_write_lock": 'FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK = "1"' in runner,
        "runner_reports_zero_order_writes": "Mainnet order requests sent by R5.1 readonly runner: 0" in runner,
        "runner_separates_stdout_and_stderr": "1> $StdoutCapture 2> $StderrCapture" in runner and "2>&1" not in runner,
        "runner_returns_scalar_exit_code": "$ExitCode = [int]$LASTEXITCODE" in runner and "return $ExitCode" in runner,
        "runner_does_not_mutate_source_credential_acl": "icacls.exe $CredentialPath" not in runner,
        "credentials_are_not_copied_into_project": "credentials_persisted_by_r51" in bootstrap and "credential_payload" not in bootstrap,
        "legacy_generated_copy_cleanup_is_exact_match_only": "_remove_previous_generated_copy" in bootstrap and "values != [key, secret, MAINNET_CREDENTIAL_MARKER]" in bootstrap,
        "r24_r25_live_account_binding": "r24_account_fingerprint" in evidence and "evidence_account_fingerprint" in orchestrator,
        "missing_account_alias_uses_exact_r24_legacy_fallback": "R24_LEGACY_MISSING_ACCOUNT_ALIAS" in texts.get("credentials", "") and "r24_account_fingerprint_candidates" in texts.get("credentials", ""),
        "live_binding_acceptance_is_explicit": "live_r24_account_bound" in acceptance and "live_evidence_binding_mode" in acceptance,
        "stream_health_and_db_integrity_are_required": "_assert_stream_health" in evidence and "reopen_readback" in evidence and "integrity_after_reopen" in evidence,
        "evidence_time_contract_is_timezone_aware": '"completed_at_utc"' in evidence and "tzinfo" in evidence and "stale or from the future" in evidence,
        "readonly_key_policy_is_promotion_only": "if stage is not R5Stage.MAINNET_READONLY" in orchestrator and "_readonly_promotion_readiness" in orchestrator,
        "readonly_stage_evidence_is_refreshable": "replace_stage_evidence=request.stage is R5Stage.MAINNET_READONLY" in orchestrator and "ON CONFLICT(account_id,stage) DO UPDATE" in journal,
        "immutable_evidence_history_supports_old_replay": "r5_stage_evidence_history" in journal and "immutable evidence" in journal,
        "order_test_requires_fresh_promotion_ready_evidence": "require_promotion_ready=stage is R5Stage.ORDER_TEST" in orchestrator and "max_age_seconds" in journal,
        "signed_retries_refresh_timestamp_and_signature": "for attempt in range(3)" in read_api and 'values["timestamp"]' in read_api and 'values["signature"]' in read_api,
        "authenticated_transport_disables_redirects": "_NoRedirectHandler" in transport,
        "acceptance_separates_key_and_account_blocks": "BLOCKED_KEY_AND_ACCOUNT_STATE" in acceptance and "BLOCKED_ACCOUNT_STATE" in acceptance,
        "exact_binance_hosts_are_fixed": "https://fapi.binance.com" in combined_readonly and "https://api.binance.com" in combined_readonly,
    }
    failures = sorted(name for name, passed in checks.items() if not passed)
    payload: dict[str, Any] = {
        "schema_version": 2,
        "toolkit_version": "5.1.6",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "R5_1_OFFLINE_ACCEPTANCE_PASS" if not failures else "R5_1_OFFLINE_ACCEPTANCE_FAIL",
        "checks": {name: "PASS" if passed else "FAIL" for name, passed in sorted(checks.items())},
        "required_failures": failures,
        "missing_files": missing,
        "forbidden_tokens": forbidden,
        "installer_network_access": "NONE",
        "real_mainnet_order_requests_sent_by_installer": 0,
        "mainnet_exchange_write": "LOCKED",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    destination = Path(output).expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="ascii")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hedge-r51-offline-acceptance")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    payload = build_report(project_root=args.project_root, output=args.output)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if not payload["required_failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
