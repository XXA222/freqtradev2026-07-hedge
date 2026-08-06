from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .binance_client import BinanceUSDMReadonlyClient
from .collector import CollectionOptions, collect_authenticated_snapshot, pair_to_symbol
from .common import atomic_write_json, sha256_json
from .persistence import verify_persistence
from .security import contains_secret_bytes, load_credentials
from .validation import required_failures
from .write_lock import ExchangeWriteLocked, install as install_write_lock


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freqtrade-hedge-r24")
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--pair", default="BTC/USDT:USDT")
    parser.add_argument("--credential-config", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--history-days", type=int, default=30)
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--clock-samples", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument(
        "--strict-key-policy",
        action="store_true",
        help="Require IP restriction and futures trading permission for production-key readiness",
    )
    parser.add_argument("--allow-multi-assets", action="store_true")
    parser.add_argument("--require-active-dual-leg", action="store_true")
    return parser


def _verify_ccxt_write_lock() -> dict[str, Any]:
    result = install_write_lock()
    if result.get("enabled") is not True:
        raise RuntimeError("FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK=1 is required")
    try:
        import ccxt
    except ImportError as exc:
        raise RuntimeError("CCXT is required for the write-lock self-test") from exc
    exchange = ccxt.binance()
    try:
        try:
            exchange.create_order("BTC/USDT:USDT", "limit", "buy", 1, 1)
        except ExchangeWriteLocked:
            return {**result, "create_order_probe": "BLOCKED"}
        raise RuntimeError("CCXT create_order was not blocked")
    finally:
        close = getattr(exchange, "close", None)
        if callable(close):
            close()


def _entity_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    names = (
        "balances",
        "positions",
        "open_orders",
        "orders",
        "trades",
        "income",
        "commission_rates",
        "leverage_brackets",
        "symbol_config",
        "order_rate_limits",
        "exchange_symbols",
    )
    return {name: len(snapshot.get(name, [])) for name in names}


def _secret_scan(paths: list[Path], api_key: str, api_secret: str) -> dict[str, Any]:
    failures: dict[str, list[str]] = {}
    expanded: list[Path] = []
    for path in paths:
        expanded.append(path)
        expanded.extend(candidate for candidate in path.parent.glob(f"{path.name}-*") if candidate.is_file())
    for path in sorted(set(expanded)):
        if not path.is_file():
            continue
        found = contains_secret_bytes(path, [api_key, api_secret])
        if found:
            failures[path.name] = found
    if failures:
        raise RuntimeError(f"Credential material leaked into generated artifacts: {failures}")
    return {"status": "PASS", "scanned_files": [path.name for path in sorted(set(expanded)) if path.is_file()]}


def _gate_domains(checks: list[Any], persistence: dict[str, Any], *, strict: bool) -> dict[str, str]:
    failed_names = {
        check.name
        for check in checks
        if check.status == "FAIL" and check.severity == "REQUIRED"
    }

    def status_for(*prefixes: str) -> str:
        return (
            "FAIL"
            if any(any(name.startswith(prefix) for prefix in prefixes) for name in failed_names)
            else "PASS"
        )

    strict_failures = {
        "permission.futures_trading",
        "permission.ip_restricted",
    } & failed_names
    return {
        "clock_readiness": status_for("clock."),
        "authenticated_read_access": status_for(
            "permission.reading", "permission.futures_private_read"
        ),
        "money_movement_safety": status_for(
            "permission.withdrawals_disabled",
            "permission.internal_transfer_disabled",
            "permission.universal_transfer_disabled",
        ),
        "account_and_hedge_semantics": status_for(
            "mode.",
            "account_config.",
            "margin.",
            "positions.",
            "account.",
            "balances.",
            "orders.",
            "exchange.",
            "reconcile.",
            "leverage.",
        ),
        "history_completeness": status_for("history."),
        "database_persistence": str(persistence.get("status", "FAIL")),
        "strict_key_policy": (
            "NOT_REQUESTED"
            if not strict
            else ("FAIL" if strict_failures else "PASS")
        ),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    credentials = load_credentials(args.credential_config)
    write_lock = _verify_ccxt_write_lock()
    client = BinanceUSDMReadonlyClient(
        credentials,
        proxy_url=args.proxy,
        timeout=args.timeout,
        max_retries=args.max_retries,
    )
    options = CollectionOptions(
        target_symbol=pair_to_symbol(args.pair),
        history_days=args.history_days,
        page_limit=args.page_limit,
        clock_samples=args.clock_samples,
        max_symbols=args.max_symbols,
        strict_key_policy=args.strict_key_policy,
        require_single_asset_mode=not args.allow_multi_assets,
        require_active_dual_leg=args.require_active_dual_leg,
    )
    snapshot, checks = collect_authenticated_snapshot(client, options)
    snapshot["safety"] = {
        "exchange_writes_locked": True,
        "signed_http_methods": ["GET"],
        "ccxt_write_lock": write_lock,
        "credential_source": credentials.source,
    }
    snapshot["snapshot_hash"] = sha256_json(
        {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    )
    atomic_write_json(args.snapshot, snapshot)
    persistence = verify_persistence(args.database, snapshot)
    artifact_secret_scan = _secret_scan(
        [args.snapshot, args.database], credentials.api_key, credentials.api_secret
    )

    failures = required_failures(checks)
    evidence = next(
        (check for check in checks if check.name == "positions.active_dual_leg"), None
    )
    if failures:
        status = "FAIL"
    elif evidence is not None and evidence.status == "NOT_OBSERVED":
        status = "PASS_WITH_DUAL_LEG_OBSERVATION_GAP"
    else:
        status = "FULL_PASS"
    check_counts = Counter(check.status for check in checks)
    gate_domains = _gate_domains(checks, persistence, strict=args.strict_key_policy)
    report = {
        "schema_version": 1,
        "toolkit_version": "2.4.1",
        "status": status,
        "mode": snapshot["mode"],
        "run_id": snapshot["run_id"],
        "created_at_utc": snapshot["created_at_utc"],
        "account_fingerprint": snapshot["account_fingerprint"],
        "key_policy": snapshot["key_policy"],
        "scope": snapshot["scope"],
        "clock": snapshot["clock"],
        "snapshot_interval": snapshot["snapshot_interval"],
        "entity_counts": _entity_counts(snapshot),
        "history_watermarks": snapshot["watermarks"],
        "required_failures": failures,
        "check_counts": dict(sorted(check_counts.items())),
        "gate_domains": gate_domains,
        "checks": [check.to_dict() for check in checks],
        "optional_endpoint_errors": snapshot["optional_endpoint_errors"],
        "transport": snapshot["transport"],
        "persistence": persistence,
        "security": {
            "exchange_writes": "LOCKED",
            "direct_http_methods": ["GET"],
            "credentials_persisted": False,
            "artifact_secret_scan": artifact_secret_scan,
        },
        "artifacts": {
            "snapshot": str(args.snapshot),
            "database": str(args.database),
            "report": str(args.report),
        },
    }
    atomic_write_json(args.report, report)
    _secret_scan([args.report], credentials.api_key, credentials.api_secret)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run(args)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] != "FAIL" else 2
    except Exception as exc:  # authenticated boundary; never include credentials in errors
        print(
            json.dumps(
                {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        for name in (
            "BINANCE_API_KEY",
            "BINANCE_API_SECRET",
            "FREQTRADE__EXCHANGE__KEY",
            "FREQTRADE__EXCHANGE__SECRET",
        ):
            if os.environ.get("R24_CLEAR_CREDENTIAL_ENV") == "1":
                os.environ.pop(name, None)


if __name__ == "__main__":
    raise SystemExit(main())
