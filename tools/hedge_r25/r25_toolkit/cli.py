from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from freqtrade.hedge.exchange.base import CalibrationKind, ReadonlyState, to_primitive
from freqtrade.hedge.exchange.binance_readonly import PermissionPolicy
from freqtrade.hedge.readonly.runtime import (
    BinanceReadonlyRuntimeConfig,
    build_binance_readonly_runtime,
)
from r24_toolkit.security import load_credentials
from r24_toolkit.write_lock import install as install_write_lock

from . import VERSION
from .managed_symbol_config_compat import (
    install_managed_symbol_config_scope_compat,
)
from .position_risk_compat import install_position_risk_compat
from .scope_compat import (
    discover_active_observation_symbols,
    install_usdm_delivery_symbol_compat,
)
from .reporting import append_jsonl, atomic_write_json, scan_artifacts, with_hash
from .sqlite_repository import DurableReadonlyRepository


_PASS_R24 = {"FULL_PASS", "PASS_WITH_DUAL_LEG_OBSERVATION_GAP"}
_SECRET_PATTERN = re.compile(
    r"(?i)(signature|api[_-]?key|secret|x-mbx-apikey)=([^&\s,}]+)"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freqtrade-hedge-r25",
        description=(
            "Run the authenticated Binance USD-M listenKey/WebSocket runtime, "
            "continuous REST reconciliation, and durable read-only observation."
        ),
    )
    parser.add_argument("--r24-report", type=Path, required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--credential-config", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=180.0)
    parser.add_argument("--observation-interval-seconds", type=float, default=10.0)
    parser.add_argument("--ready-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--fast-interval-seconds", type=float, default=60.0)
    parser.add_argument("--full-interval-seconds", type=float, default=900.0)
    parser.add_argument("--error-retry-seconds", type=float, default=30.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=25.0)
    parser.add_argument("--max-collection-span-seconds", type=float, default=45.0)
    parser.add_argument("--max-clock-skew-ms", type=float, default=1000.0)
    parser.add_argument("--fill-lookback-hours", type=float, default=72.0)
    parser.add_argument("--history-overlap-minutes", type=float, default=5.0)
    parser.add_argument("--max-history-backfill-days", type=float, default=30.0)
    parser.add_argument("--exercise-reconnect", action="store_true")
    parser.add_argument("--require-user-event", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def _positive(value: float, name: str, *, minimum: float = 0.0) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= minimum:
        raise ValueError(f"{name} must be finite and greater than {minimum}")
    return parsed


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must contain an object: {path}")
    return value


def _validate_r24_report(path: Path) -> dict[str, Any]:
    report = _read_json(path)
    status = str(report.get("status", ""))
    if status not in _PASS_R24:
        raise ValueError(f"R2.4.1 prerequisite is not accepted: {status or 'MISSING'}")
    security = report.get("security")
    if not isinstance(security, Mapping) or security.get("exchange_writes") != "LOCKED":
        raise ValueError("R2.4.1 prerequisite does not prove exchange writes are LOCKED")
    gates = report.get("gate_domains")
    required_domains = (
        "clock_readiness",
        "authenticated_read_access",
        "account_and_hedge_semantics",
        "database_persistence",
        "history_completeness",
        "money_movement_safety",
    )
    if not isinstance(gates, Mapping):
        raise ValueError("R2.4.1 gate_domains are missing")
    failed = [name for name in required_domains if gates.get(name) != "PASS"]
    if failed:
        raise ValueError(f"R2.4.1 required gate domain(s) failed: {failed}")
    scope = report.get("scope")
    symbols = () if not isinstance(scope, Mapping) else scope.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("R2.4.1 report does not contain a nonempty symbol scope")
    fingerprint = str(report.get("account_fingerprint", "")).strip()
    if not fingerprint:
        raise ValueError("R2.4.1 report does not contain account_fingerprint")
    return report


def _safe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}:{exc}"
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", text)[:1000]


def _observation(snapshot: Any, *, sequence: int, phase: str) -> dict[str, Any]:
    account_view = snapshot.account_view
    stream = snapshot.stream_health
    transport = snapshot.transport_telemetry
    health = snapshot.direction2_health
    return {
        "schema_version": 1,
        "toolkit_version": VERSION,
        "sequence": sequence,
        "phase": phase,
        "observed_at": datetime.now(UTC),
        "service": {
            "state": snapshot.status.state.value,
            "reason": snapshot.status.reason,
            "changed_at": snapshot.status.changed_at,
            "last_calibration_run_id": snapshot.status.last_calibration_run_id,
        },
        "stream": {
            "connected": stream.connected,
            "last_connected_at": stream.last_connected_at,
            "last_event_at": stream.last_event_at,
            "last_calibration_at": stream.last_calibration_at,
            "reconnect_count": stream.reconnect_count,
            "duplicate_count": stream.duplicate_count,
            "out_of_order_count": stream.out_of_order_count,
            "gap_count": stream.gap_count,
        },
        "freshness": to_primitive(snapshot.freshness),
        "direction2_health": to_primitive(health),
        "transport": None if transport is None else to_primitive(transport),
        "clock_sync": (
            None
            if snapshot.clock_sync_status is None
            else to_primitive(snapshot.clock_sync_status)
        ),
        "listen_key": {
            "generation": snapshot.listen_key_generation,
            "created_at": snapshot.listen_key_created_at,
            "renewed_at": snapshot.listen_key_renewed_at,
            "expires_at": snapshot.listen_key_expires_at,
        },
        "account_view": {
            "revision": None if account_view is None else account_view.revision,
            "balance_count": 0 if account_view is None else len(account_view.balances),
            "position_count": 0 if account_view is None else len(account_view.positions),
            "active_order_count": (
                0 if account_view is None else len(account_view.active_orders)
            ),
            "last_calibration_run_id": (
                None if account_view is None else account_view.last_calibration_run_id
            ),
        },
        "calibrations": {
            "fast": (
                None
                if snapshot.last_fast_calibration is None
                else to_primitive(snapshot.last_fast_calibration)
            ),
            "full": (
                None
                if snapshot.last_full_calibration is None
                else to_primitive(snapshot.last_full_calibration)
            ),
            "reconnect": (
                None
                if snapshot.last_reconnect_calibration is None
                else to_primitive(snapshot.last_reconnect_calibration)
            ),
        },
    }


async def _observe(args: argparse.Namespace) -> dict[str, Any]:
    duration = _positive(args.duration_seconds, "duration-seconds")
    observation_interval = _positive(
        args.observation_interval_seconds,
        "observation-interval-seconds",
    )
    ready_timeout = _positive(args.ready_timeout_seconds, "ready-timeout-seconds")
    fast_interval = _positive(args.fast_interval_seconds, "fast-interval-seconds")
    full_interval = _positive(args.full_interval_seconds, "full-interval-seconds")
    if full_interval < fast_interval:
        raise ValueError("full-interval-seconds must be >= fast-interval-seconds")

    r24 = _validate_r24_report(args.r24_report.resolve())
    credentials = load_credentials(args.credential_config)
    write_lock = install_write_lock()
    if write_lock.get("enabled") is not True:
        raise RuntimeError("FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK=1 is required")

    scope = r24["scope"]
    r24_symbols = tuple(str(value) for value in scope["symbols"])
    account_fingerprint = str(r24["account_fingerprint"])
    account_id = f"binance-usdm:{account_fingerprint}"
    delivery_symbol_compat = install_usdm_delivery_symbol_compat()
    managed_symbol_config_compat = (
        install_managed_symbol_config_scope_compat()
    )
    scope_discovery = await discover_active_observation_symbols(
        account_id=account_id,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        initial_symbols=r24_symbols,
        proxy_url=args.proxy,
        request_timeout_seconds=_positive(
            args.request_timeout_seconds,
            "request-timeout-seconds",
        ),
        max_clock_skew_ms=_positive(args.max_clock_skew_ms, "max-clock-skew-ms"),
    )
    symbols = tuple(scope_discovery["runtime_symbols"])
    run_id = f"r25-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:12]}"

    database = args.database.resolve()
    observations = args.observations.resolve()
    report_path = args.report.resolve()
    observations.parent.mkdir(parents=True, exist_ok=True)
    if observations.exists():
        observations.unlink()

    repository = DurableReadonlyRepository(database)
    repository.start_run(
        run_id,
        datetime.now(UTC),
        {
            "toolkit_version": VERSION,
            "account_fingerprint": account_fingerprint,
            "r24_symbols": r24_symbols,
            "runtime_symbols": symbols,
            "auto_observed_symbols": scope_discovery["auto_observed_symbols"],
            "r24_report": str(args.r24_report.resolve()),
            "exchange_writes": "LOCKED",
        },
    )

    config = BinanceReadonlyRuntimeConfig(
        account_id=account_id,
        managed_symbols=symbols,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        rest_proxy_url=args.proxy,
        websocket_proxy_url=args.proxy,
        request_timeout_seconds=_positive(
            args.request_timeout_seconds,
            "request-timeout-seconds",
        ),
        max_clock_skew_ms=_positive(args.max_clock_skew_ms, "max-clock-skew-ms"),
        max_collection_span_seconds=_positive(
            args.max_collection_span_seconds,
            "max-collection-span-seconds",
        ),
        fast_calibration_interval=timedelta(seconds=fast_interval),
        full_calibration_interval=timedelta(seconds=full_interval),
        calibration_error_retry_interval=timedelta(
            seconds=_positive(args.error_retry_seconds, "error-retry-seconds")
        ),
        event_stale_after=None,
        calibration_stale_after=timedelta(
            seconds=max(full_interval * 2.0, full_interval + 60.0)
        ),
        fill_lookback=timedelta(
            hours=_positive(args.fill_lookback_hours, "fill-lookback-hours")
        ),
        history_overlap=timedelta(
            minutes=_positive(
                args.history_overlap_minutes,
                "history-overlap-minutes",
                minimum=-1.0,
            )
        ),
        max_history_backfill=timedelta(
            days=_positive(
                args.max_history_backfill_days,
                "max-history-backfill-days",
            )
        ),
        permission_policy=PermissionPolicy(require_futures_enabled=False),
    )
    runtime = build_binance_readonly_runtime(config=config, repository=repository)
    runtime_client = getattr(runtime, "client", None)
    if runtime_client is None:
        position_risk_compat = {
            "installed": False,
            "reason": "RUNTIME_CLIENT_UNAVAILABLE",
        }
    else:
        position_risk_compat = install_position_risk_compat(runtime_client)

    sequence = 0
    observations_memory: list[dict[str, Any]] = []
    ready_seen = False
    connected_seen = False
    reconnect_requested = False
    reconnect_recovered = False
    initial_reconnect_count = 0
    final_snapshot = None
    error: str | None = None
    started_at = datetime.now(UTC)
    try:
        await runtime.start()
        await runtime.wait_until_ready(timeout_seconds=ready_timeout)
        ready_seen = True
        first_snapshot = runtime.snapshot()
        connected_seen = first_snapshot.stream_health.connected
        initial_reconnect_count = first_snapshot.stream_health.reconnect_count
        deadline = runtime.clock.monotonic() + duration
        reconnect_deadline = runtime.clock.monotonic() + max(
            observation_interval,
            min(duration / 3.0, 60.0),
        )
        while True:
            snapshot = runtime.snapshot()
            connected_seen = connected_seen or snapshot.stream_health.connected
            ready_seen = ready_seen or snapshot.status.state is ReadonlyState.READY
            phase = "OBSERVE"
            if (
                args.exercise_reconnect
                and not reconnect_requested
                and runtime.clock.monotonic() >= reconnect_deadline
            ):
                reconnect_requested = True
                phase = "RECONNECT_REQUESTED"
                await runtime.request_reconnect()
            if reconnect_requested and (
                snapshot.stream_health.reconnect_count > initial_reconnect_count
                and snapshot.status.state is ReadonlyState.READY
                and snapshot.last_reconnect_calibration is not None
            ):
                reconnect_recovered = True
            item = _observation(snapshot, sequence=sequence, phase=phase)
            repository.record_observation(run_id, sequence, datetime.now(UTC), item)
            append_jsonl(observations, item)
            observations_memory.append(item)
            sequence += 1
            if snapshot.status.state is ReadonlyState.HALT:
                break
            remaining = deadline - runtime.clock.monotonic()
            if remaining <= 0:
                break
            await runtime.clock.sleep(min(observation_interval, remaining))
        if runtime.snapshot().status.state not in {ReadonlyState.HALT, ReadonlyState.STOPPED}:
            await runtime.calibrate_now(CalibrationKind.FAST)
        final_snapshot = runtime.snapshot()
    except Exception as exc:
        error = _safe_error(exc)
        try:
            final_snapshot = runtime.snapshot()
        except Exception:
            final_snapshot = None
    finally:
        try:
            await runtime.stop()
        except Exception as exc:
            if error is None:
                error = _safe_error(exc)

    completed_at = datetime.now(UTC)
    final_observation = (
        None
        if final_snapshot is None
        else _observation(final_snapshot, sequence=sequence, phase="FINAL")
    )
    if final_observation is not None:
        repository.record_observation(run_id, sequence, completed_at, final_observation)
        append_jsonl(observations, final_observation)
        observations_memory.append(final_observation)

    counts_before_reopen = repository.counts()
    integrity_before_reopen = repository.integrity()
    source_counts = repository.event_source_counts()
    repository.close()

    reopened = DurableReadonlyRepository(database)
    counts_after_reopen = reopened.counts()
    integrity_after_reopen = reopened.integrity()
    active_positions_after_reopen = len(await reopened.load_active_positions(account_id))
    active_orders_after_reopen = len(await reopened.load_active_orders(account_id))
    reopened.close()

    final_stream = None if final_snapshot is None else final_snapshot.stream_health
    final_health = None if final_snapshot is None else final_snapshot.direction2_health
    final_state = (
        "UNAVAILABLE" if final_snapshot is None else final_snapshot.status.state.value
    )
    required_failures: list[str] = []
    if error is not None:
        required_failures.append("RUNTIME_EXCEPTION")
    if not ready_seen:
        required_failures.append("READY_NOT_OBSERVED")
    if not connected_seen:
        required_failures.append("WEBSOCKET_CONNECTION_NOT_OBSERVED")
    if final_stream is not None and final_stream.gap_count:
        required_failures.append("USER_STREAM_GAP_DETECTED")
    if final_stream is not None and final_stream.out_of_order_count:
        required_failures.append("USER_STREAM_OUT_OF_ORDER_DETECTED")
    if final_state == ReadonlyState.HALT.value:
        required_failures.append("RUNTIME_HALTED")
    if integrity_before_reopen["status"] != "PASS":
        required_failures.append("SQLITE_INTEGRITY_FAILED")
    if integrity_after_reopen["status"] != "PASS":
        required_failures.append("SQLITE_REOPEN_INTEGRITY_FAILED")
    if counts_before_reopen != counts_after_reopen:
        required_failures.append("SQLITE_REOPEN_COUNT_MISMATCH")
    if args.exercise_reconnect and not reconnect_recovered:
        required_failures.append("FORCED_RECONNECT_NOT_RECOVERED")

    user_stream_fact_count = int(source_counts.get("BINANCE_USER_STREAM", 0))
    if args.require_user_event and user_stream_fact_count == 0:
        required_failures.append("USER_EVENT_NOT_OBSERVED")
    required_failures = list(dict.fromkeys(required_failures))
    if required_failures:
        status = "FAIL"
    elif user_stream_fact_count > 0:
        status = "FULL_PASS"
    else:
        status = "PASS_WITH_NO_USER_EVENTS"

    transport = None if final_snapshot is None else final_snapshot.transport_telemetry
    report = with_hash(
        {
            "schema_version": 1,
            "toolkit_version": VERSION,
            "status": status,
            "run_id": run_id,
            "started_at_utc": started_at,
            "completed_at_utc": completed_at,
            "duration_seconds": (completed_at - started_at).total_seconds(),
            "r24_prerequisite": {
                "status": r24["status"],
                "run_id": r24.get("run_id"),
                "report": str(args.r24_report.resolve()),
            },
            "scope": {
                "account_fingerprint": account_fingerprint,
                "account_id": account_id,
                "r24_symbols": r24_symbols,
                "symbols": symbols,
                "auto_observed_symbols": scope_discovery["auto_observed_symbols"],
                "active_position_symbols": scope_discovery["active_position_symbols"],
                "active_order_symbols": scope_discovery["active_order_symbols"],
            },
            "result": {
                "required_failures": required_failures,
                "error": error,
                "ready_observed": ready_seen,
                "websocket_connected_observed": connected_seen,
                "forced_reconnect_requested": reconnect_requested,
                "forced_reconnect_recovered": reconnect_recovered,
                "user_stream_fact_count": user_stream_fact_count,
                "observation_count": len(observations_memory),
                "final_state": final_state,
            },
            "stream": None if final_stream is None else to_primitive(final_stream),
            "direction2_health": (
                None if final_health is None else to_primitive(final_health)
            ),
            "transport": None if transport is None else to_primitive(transport),
            "database": {
                "path": str(database),
                "counts_before_reopen": counts_before_reopen,
                "counts_after_reopen": counts_after_reopen,
                "integrity_before_reopen": integrity_before_reopen,
                "integrity_after_reopen": integrity_after_reopen,
                "active_positions_after_reopen": active_positions_after_reopen,
                "active_orders_after_reopen": active_orders_after_reopen,
                "event_source_counts": source_counts,
                "reopen_readback": (
                    "PASS"
                    if counts_before_reopen == counts_after_reopen
                    else "FAIL"
                ),
            },
            "security": {
                "exchange_order_writes": "LOCKED",
                "credentials_persisted": False,
                "permission_profile": "READONLY_USER_STREAM",
                "futures_trading_permission_required": False,
                "allowed_control_plane_writes": [
                    "POST /fapi/v1/listenKey",
                    "PUT /fapi/v1/listenKey",
                    "DELETE /fapi/v1/listenKey",
                ],
                "trading_endpoints_allowed": False,
                "write_lock": write_lock,
                "position_risk_compat": position_risk_compat,
                "delivery_symbol_compat": delivery_symbol_compat,
                "managed_symbol_config_compat": managed_symbol_config_compat,
                "scope_discovery": {
                    "mode": "READONLY_ACTIVE_SYMBOL_AUTO_OBSERVATION",
                    "auto_observed_symbols": scope_discovery["auto_observed_symbols"],
                    "exchange_order_writes": "LOCKED",
                },
            },
            "artifacts": {
                "database": str(database),
                "observations": str(observations),
                "report": str(report_path),
            },
        }
    )
    atomic_write_json(report_path, report)
    secret_scan = scan_artifacts(
        [database, observations, report_path],
        (credentials.api_key, credentials.api_secret),
    )
    report["security"]["artifact_secret_scan"] = secret_scan
    report = with_hash({key: value for key, value in report.items() if key != "report_hash"})
    atomic_write_json(report_path, report)

    repository_for_completion = DurableReadonlyRepository(database)
    repository_for_completion.complete_run(
        run_id,
        completed_at=completed_at,
        status=status,
        payload=report,
    )
    repository_for_completion.close()
    scan_artifacts(
        [database, observations, report_path],
        (credentials.api_key, credentials.api_secret),
    )
    return report


def run(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_observe(args))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        report = run(args)
        print(json.dumps(to_primitive(report), ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] != "FAIL" else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "toolkit_version": VERSION,
                    "status": "FAIL",
                    "error": _safe_error(exc),
                    "exchange_order_writes": "LOCKED",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
