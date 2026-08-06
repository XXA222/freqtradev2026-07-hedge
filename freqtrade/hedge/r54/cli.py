from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from freqtrade.hedge.r5.models import R5Stage

from .auto import AutoObservationResult, build_auto_observation_evidence
from .backup import run_backup_restore_drill
from .bundle import build_evidence_bundle, verify_evidence_bundle
from .canary import build_canary_evidence, parse_canary_result
from .canonical import atomic_write_json
from .config_validation import build_config_validation_evidence
from .evidence import load_envelope, save_envelope, verify_envelope
from .models import ArtifactKind
from .postgres import PostgresMatrixResult, build_postgres_evidence
from .preflight import MainnetReadonlyPreflightResult, build_mainnet_preflight_evidence
from .promotion import R54PromotionEngine
from .readiness import R54ReadinessEvaluator
from .soak import build_soak_evidence
from .store import R54OperationalStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freqtrade-hedge-r54")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-store")
    init.add_argument("--database", required=True)

    record = sub.add_parser("record-evidence")
    record.add_argument("--database", required=True)
    record.add_argument("--evidence", required=True)
    record.add_argument("--account-id", required=True)

    evaluate = sub.add_parser("evaluate-stage")
    evaluate.add_argument("--database", required=True)
    evaluate.add_argument("--account-id", required=True)
    evaluate.add_argument("--stage", required=True, choices=[item.value for item in R5Stage])
    evaluate.add_argument("--output")

    evaluate_all = sub.add_parser("evaluate-all")
    evaluate_all.add_argument("--database", required=True)
    evaluate_all.add_argument("--account-id", required=True)
    evaluate_all.add_argument("--output")

    soak = sub.add_parser("evaluate-soak")
    soak.add_argument("--observations", required=True)
    soak.add_argument("--account-id", required=True)
    soak.add_argument("--output-24h", required=True)
    soak.add_argument("--output-72h")
    soak.add_argument("--expected-interval-seconds", type=float, default=60.0)

    preflight = sub.add_parser("build-preflight")
    preflight.add_argument("--input", required=True)
    preflight.add_argument("--account-id", required=True)
    preflight.add_argument("--output", required=True)
    preflight.add_argument("--ttl-seconds", type=int, default=900)

    postgres = sub.add_parser("build-postgres")
    postgres.add_argument("--input", required=True)
    postgres.add_argument("--account-id", required=True)
    postgres.add_argument("--output", required=True)

    canary = sub.add_parser("build-canary")
    canary.add_argument("--input", required=True)
    canary.add_argument("--account-id", required=True)
    canary.add_argument("--environment", required=True, choices=("TESTNET", "MAINNET"))
    canary.add_argument(
        "--kind",
        required=True,
        choices=[
            item.value
            for item in ArtifactKind
            if "TEST" in item.value or "LEG" in item.value
        ],
    )
    canary.add_argument("--max-notional", required=True)
    canary.add_argument("--require-flat-finish", action="store_true")
    canary.add_argument("--output", required=True)

    auto = sub.add_parser("build-auto-observation")
    auto.add_argument("--input", required=True)
    auto.add_argument("--account-id", required=True)
    auto.add_argument("--hours", type=int, choices=(24, 72), required=True)
    auto.add_argument("--output", required=True)

    backup = sub.add_parser("backup-drill")
    backup.add_argument("--source", required=True)
    backup.add_argument("--account-id", required=True)
    backup.add_argument("--output", required=True)

    config = sub.add_parser("validate-config-manifest")
    config.add_argument("--manifest", required=True)
    config.add_argument("--config-root", required=True)
    config.add_argument("--account-id", required=True)
    config.add_argument("--output", required=True)

    bundle = sub.add_parser("bundle")
    bundle.add_argument("--output", required=True)
    bundle.add_argument("--account-id", required=True)
    bundle.add_argument("files", nargs="+")

    verify = sub.add_parser("verify-bundle")
    verify.add_argument("--bundle", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init-store":
        store = R54OperationalStore(args.database)
        ok, detail = store.integrity_check()
        return _print({"status": "PASS" if ok else "FAIL", "integrity": detail}, ok)
    if args.command == "record-evidence":
        envelope = load_envelope(args.evidence)
        verify_envelope(envelope, expected_account_id=args.account_id)
        evidence_id = R54OperationalStore(args.database).put_evidence(envelope)
        return _print({"status": "PASS", "evidence_id": evidence_id}, True)
    if args.command == "evaluate-stage":
        decision = R54PromotionEngine(R54OperationalStore(args.database)).evaluate(
            account_id=args.account_id,
            stage=R5Stage(args.stage),
        )
        payload = decision.to_dict()
        if args.output:
            atomic_write_json(args.output, payload)
        return _print(payload, decision.ready)
    if args.command == "evaluate-all":
        decisions = R54ReadinessEvaluator(R54OperationalStore(args.database)).evaluate_all(
            account_id=args.account_id
        )
        payload = {
            "account_id": args.account_id,
            "decisions": [item.to_dict() for item in decisions],
            "highest_ready_stage": next(
                (item.stage.value for item in reversed(decisions) if item.ready), None
            ),
        }
        if args.output:
            atomic_write_json(args.output, payload)
        return _print(payload, all(item.ready for item in decisions))
    if args.command == "evaluate-soak":
        evidence_24, evidence_72 = build_soak_evidence(
            observations_path=args.observations,
            account_id=args.account_id,
            expected_interval_seconds=args.expected_interval_seconds,
        )
        save_envelope(args.output_24h, evidence_24)
        if args.output_72h and evidence_72 is not None:
            save_envelope(args.output_72h, evidence_72)
        return _print(
            {"status_24h": evidence_24.status.value, "has_72h": evidence_72 is not None},
            evidence_24.status.value == "PASS",
        )
    if args.command == "build-preflight":
        raw = _load_object(args.input)
        result = MainnetReadonlyPreflightResult(
            private_api_ok=bool(raw["private_api_ok"]),
            hedge_mode=bool(raw["hedge_mode"]),
            cross_symbols=tuple(str(item) for item in raw["cross_symbols"]),
            required_cross_symbols=tuple(str(item) for item in raw["required_cross_symbols"]),
            clock_offset_ms=int(raw["clock_offset_ms"]),
            max_abs_clock_offset_ms=int(raw["max_abs_clock_offset_ms"]),
            user_stream_connected=bool(raw["user_stream_connected"]),
            user_stream_fresh=bool(raw["user_stream_fresh"]),
            gap_detected=bool(raw["gap_detected"]),
            reconciliation_diff_count=int(raw["reconciliation_diff_count"]),
            unknown_order_count=int(raw["unknown_order_count"]),
            database_persistent=bool(raw["database_persistent"]),
            restart_replay_ok=bool(raw["restart_replay_ok"]),
            exchange_writes_locked=bool(raw["exchange_writes_locked"]),
            account_fingerprint=str(raw["account_fingerprint"]),
        )
        evidence = build_mainnet_preflight_evidence(
            result,
            account_id=args.account_id,
            ttl_seconds=args.ttl_seconds,
        )
        save_envelope(args.output, evidence)
        return _print(evidence.to_dict(), evidence.status.value == "PASS")
    if args.command == "build-postgres":
        raw = _load_object(args.input)
        result = PostgresMatrixResult(
            checks=dict(raw["checks"]),  # type: ignore[arg-type]
            pytest_passed=int(raw["pytest_passed"]),
            pytest_failed=int(raw["pytest_failed"]),
            server_version=str(raw["server_version"]),
            dsn_redacted=str(raw["dsn_redacted"]),
        )
        evidence = build_postgres_evidence(result, account_id=args.account_id)
        save_envelope(args.output, evidence)
        return _print(evidence.to_dict(), evidence.status.value == "PASS")
    if args.command == "build-canary":
        result = parse_canary_result(_load_object(args.input))
        evidence = build_canary_evidence(
            kind=ArtifactKind(args.kind),
            result=result,
            account_id=args.account_id,
            environment=args.environment,
            max_allowed_notional=Decimal(args.max_notional),
            require_flat_finish=args.require_flat_finish,
        )
        save_envelope(args.output, evidence)
        return _print(evidence.to_dict(), evidence.status.value == "PASS")
    if args.command == "build-auto-observation":
        raw = _load_object(args.input)
        result = AutoObservationResult(
            duration_hours=float(raw["duration_hours"]),
            cycle_count=int(raw["cycle_count"]),
            write_request_count=int(raw["write_request_count"]),
            unknown_order_count=int(raw["unknown_order_count"]),
            non_ready_cycle_count=int(raw["non_ready_cycle_count"]),
            reconciliation_diff_count=int(raw["reconciliation_diff_count"]),
            budget_breach_count=int(raw["budget_breach_count"]),
            alert_count=int(raw["alert_count"]),
            uncontained_incident_count=int(raw["uncontained_incident_count"]),
            realized_loss=Decimal(str(raw["realized_loss"])),
            maximum_drawdown=Decimal(str(raw["maximum_drawdown"])),
            limit_realized_loss=Decimal(str(raw["limit_realized_loss"])),
            limit_maximum_drawdown=Decimal(str(raw["limit_maximum_drawdown"])),
        )
        evidence = build_auto_observation_evidence(
            result, account_id=args.account_id, minimum_hours=args.hours
        )
        save_envelope(args.output, evidence)
        return _print(evidence.to_dict(), evidence.status.value == "PASS")
    if args.command == "backup-drill":
        evidence = run_backup_restore_drill(args.source, account_id=args.account_id)
        save_envelope(args.output, evidence)
        return _print(evidence.to_dict(), evidence.status.value == "PASS")
    if args.command == "validate-config-manifest":
        evidence = build_config_validation_evidence(
            manifest_path=args.manifest,
            config_root=args.config_root,
            account_id=args.account_id,
        )
        save_envelope(args.output, evidence)
        return _print(evidence.to_dict(), evidence.status.value == "PASS")
    if args.command == "bundle":
        output = build_evidence_bundle(args.output, args.files, account_id=args.account_id)
        return _print({"status": "PASS", "output": str(output)}, True)
    if args.command == "verify-bundle":
        passed, errors = verify_evidence_bundle(args.bundle)
        return _print({"status": "PASS" if passed else "FAIL", "errors": errors}, passed)
    raise AssertionError("unreachable")


def _load_object(path: str | Path) -> Mapping[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, Mapping):
        raise ValueError("input JSON must contain an object")
    return raw


def _print(payload: Mapping[str, Any], passed: bool) -> int:
    print(json.dumps(payload, sort_keys=True, default=str))
    return 0 if passed else 2
