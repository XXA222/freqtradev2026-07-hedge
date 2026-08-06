from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from freqtrade.hedge.readonly.soak_monitor import SoakMonitor, SoakObservation
from freqtrade.hedge.r5.models import R5Stage
from freqtrade.hedge.r54.auto import AutoObservationResult, build_auto_observation_evidence
from freqtrade.hedge.r54.backup import run_backup_restore_drill
from freqtrade.hedge.r54.bundle import build_evidence_bundle, verify_evidence_bundle
from freqtrade.hedge.r54.canary import CanaryResult, build_canary_evidence
from freqtrade.hedge.r54.canonical import sha256_json
from freqtrade.hedge.r54.config_validation import validate_config_manifest
from freqtrade.hedge.r54.evidence import create_envelope, load_envelope, save_envelope
from freqtrade.hedge.r54.incident import IncidentDrill, IncidentState
from freqtrade.hedge.r54.models import ArtifactKind, CheckStatus, GateStatus
from freqtrade.hedge.r54.observability import OperationalMetrics, build_alert_drill_evidence
from freqtrade.hedge.r54.postgres import PostgresMatrixResult, build_postgres_evidence
from freqtrade.hedge.r54.preflight import (
    MainnetReadonlyPreflightResult,
    build_mainnet_preflight_evidence,
)
from freqtrade.hedge.r54.promotion import EVIDENCE_RULES, POLICIES, R54PromotionEngine
from freqtrade.hedge.r54.resources import ResourceSample, build_resource_soak_evidence
from freqtrade.hedge.r54.runner import CommandSpec, run_continue_all
from freqtrade.hedge.r54.soak import build_soak_evidence
from freqtrade.hedge.r54.store import R54OperationalStore
from freqtrade.hedge.r54.upstream import UpstreamFileExpectation, verify_upstream_files


def _record(checks: list[dict[str, object]], name: str, passed: bool, detail: str = "") -> None:
    checks.append({"name": name, "status": "PASS" if passed else "FAIL", "detail": detail})


def _soak(path: Path, hours: int, *, funding: bool) -> None:
    monitor = SoakMonitor(path, expected_interval_seconds=3600.0)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for hour in range(hours + 1):
        funding_now = funding and hour == 24
        income = Decimal("0.1") if funding_now else Decimal("0")
        monitor.append(
            SoakObservation(
                observed_at=start + timedelta(hours=hour),
                service_state="READY",
                stream_age_seconds=1.0,
                fast_diff_count=0,
                full_diff_count=0,
                wallet_delta=income,
                funding_income=income,
                realized_pnl=Decimal("0"),
                commissions=Decimal("0"),
                transfers=Decimal("0"),
                funding_period_observed=funding_now,
                funding_event_count_delta=1 if funding_now else 0,
            )
        )


def run(output_path: Path | None = None) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    account_id = "offline-account"
    with tempfile.TemporaryDirectory(prefix="r54-offline-") as temporary:
        root = Path(temporary)

        _record(
            checks,
            "01_canonical_hash",
            sha256_json({"b": 2, "a": 1}) == sha256_json({"a": 1, "b": 2}),
        )

        envelope = create_envelope(
            kind=ArtifactKind.MAINNET_READONLY_PREFLIGHT,
            account_id=account_id,
            environment="MAINNET",
            producer="offline",
            payload={"ok": True},
            ttl_seconds=300,
        )
        envelope_path = root / "envelope.json"
        save_envelope(envelope_path, envelope)
        _record(checks, "02_evidence_roundtrip", load_envelope(envelope_path) == envelope)

        store = R54OperationalStore(root / "authority.sqlite")
        first = store.put_evidence(envelope)
        second = store.put_evidence(envelope)
        _record(
            checks,
            "03_sqlite_evidence_idempotency",
            first == second and store.integrity_check()[0],
        )

        preflight = MainnetReadonlyPreflightResult(
            private_api_ok=True,
            hedge_mode=True,
            cross_symbols=("BTCUSDT", "ETHUSDT"),
            required_cross_symbols=("BTCUSDT", "ETHUSDT"),
            clock_offset_ms=5,
            max_abs_clock_offset_ms=1000,
            user_stream_connected=True,
            user_stream_fresh=True,
            gap_detected=False,
            reconciliation_diff_count=0,
            unknown_order_count=0,
            database_persistent=True,
            restart_replay_ok=True,
            exchange_writes_locked=True,
            account_fingerprint="f" * 64,
        )
        preflight_evidence = build_mainnet_preflight_evidence(preflight, account_id=account_id)
        _record(
            checks,
            "04_mainnet_readonly_preflight_gate",
            preflight_evidence.status is CheckStatus.PASS,
        )

        soak24 = root / "soak24.jsonl"
        _soak(soak24, 24, funding=False)
        evidence24, no72 = build_soak_evidence(
            observations_path=soak24,
            account_id=account_id,
            expected_interval_seconds=3600.0,
        )
        _record(
            checks,
            "05_readonly_24h_gate",
            evidence24.status is CheckStatus.PASS and no72 is None,
        )

        soak72 = root / "soak72.jsonl"
        _soak(soak72, 72, funding=True)
        evidence24b, evidence72 = build_soak_evidence(
            observations_path=soak72,
            account_id=account_id,
            expected_interval_seconds=3600.0,
        )
        _record(
            checks,
            "06_readonly_72h_funding_gate",
            evidence24b.status is CheckStatus.PASS
            and evidence72 is not None
            and evidence72.status is CheckStatus.PASS,
        )

        pg_checks = {
            "fresh_migration": True,
            "idempotent_upgrade": True,
            "upgrade_downgrade_upgrade": True,
            "schema_parity": True,
            "constraint_parity": True,
            "concurrent_writer_fencing": True,
            "outbox_recovery": True,
            "backup_restore": True,
        }
        pg = build_postgres_evidence(
            PostgresMatrixResult(pg_checks, 50, 0, "16", "postgresql://user:***@host/db"),
            account_id=account_id,
        )
        _record(checks, "07_postgres_matrix_gate", pg.status is CheckStatus.PASS)

        backup_root = root / "backup-source"
        backup_root.mkdir()
        (backup_root / "ledger.txt").write_text("ledger", encoding="utf-8")
        backup = run_backup_restore_drill(backup_root, account_id=account_id)
        _record(checks, "08_backup_restore_drill", backup.status is CheckStatus.PASS)

        canary = CanaryResult(1, 1, 1, 0, Decimal("5"), True, 0, 0)
        canary_kinds = (
            ArtifactKind.TESTNET_ORDER_TEST,
            ArtifactKind.TESTNET_SUBMIT_CANCEL,
            ArtifactKind.TESTNET_SINGLE_LEG,
            ArtifactKind.TESTNET_DUAL_LEG,
        )
        for index, kind in enumerate(canary_kinds, start=9):
            evidence = build_canary_evidence(
                kind=kind,
                result=canary,
                account_id=account_id,
                environment="TESTNET",
                max_allowed_notional=Decimal("10"),
                require_flat_finish=True,
            )
            _record(
                checks,
                f"{index:02d}_{kind.value.lower()}_gate",
                evidence.status is CheckStatus.PASS,
            )

        mainnet_kinds = (
            ArtifactKind.MAINNET_ORDER_TEST,
            ArtifactKind.MAINNET_SUBMIT_CANCEL,
            ArtifactKind.MAINNET_SINGLE_LEG,
            ArtifactKind.MAINNET_DUAL_LEG,
        )
        mainnet_evidence = []
        for kind in mainnet_kinds:
            mainnet_evidence.append(
                build_canary_evidence(
                    kind=kind,
                    result=canary,
                    account_id=account_id,
                    environment="MAINNET",
                    max_allowed_notional=Decimal("10"),
                    require_flat_finish=True,
                )
            )
        _record(
            checks,
            "13_mainnet_canary_models",
            all(item.status is CheckStatus.PASS for item in mainnet_evidence),
        )

        auto = AutoObservationResult(
            72,
            4320,
            10,
            0,
            0,
            0,
            0,
            1,
            0,
            Decimal("1"),
            Decimal("2"),
            Decimal("5"),
            Decimal("10"),
        )
        auto24 = build_auto_observation_evidence(
            auto, account_id=account_id, minimum_hours=24
        )
        auto72 = build_auto_observation_evidence(
            auto, account_id=account_id, minimum_hours=72
        )
        _record(checks, "14_restricted_auto_24h_gate", auto24.status is CheckStatus.PASS)
        _record(checks, "15_restricted_auto_72h_gate", auto72.status is CheckStatus.PASS)

        normal = OperationalMetrics(1, 5, 0, 0, 0, 100, 1000, Decimal("0.1"))
        injected = OperationalMetrics(100, 5, 0, 0, 0, 100, 1000, Decimal("0.1"))
        alert = build_alert_drill_evidence(
            account_id=account_id,
            normal_metrics=normal,
            injected_metrics=injected,
        )
        _record(checks, "16_alert_drill", alert.status is CheckStatus.PASS)

        now = datetime.now(UTC)
        resources = build_resource_soak_evidence(
            (
                ResourceSample(now, 100, 10, 5, 1000),
                ResourceSample(now + timedelta(hours=1), 110, 11, 6, 1200),
            ),
            account_id=account_id,
        )
        _record(checks, "17_resource_soak", resources.status is CheckStatus.PASS)

        incident = IncidentDrill.open(incident_id="offline", account_id=account_id, reason="drill")
        incident.transition(IncidentState.CONTAINED, actor="operator", note="kill switch")
        incident.transition(IncidentState.RECOVERED, actor="operator", note="reconciled")
        incident.transition(IncidentState.CLOSED, actor="risk", note="reviewed")
        incident_evidence = incident.evidence()
        _record(checks, "18_incident_runbook", incident_evidence.status is CheckStatus.PASS)

        config_root = root / "configs"
        config_root.mkdir()
        (config_root / "config.json").write_text("{}", encoding="utf-8")
        config_manifest = config_root / "manifest.json"
        config_manifest.write_text(
            json.dumps({"entries": [{"path": "config.json", "type": "FULL_CONFIG"}]}),
            encoding="utf-8",
        )
        config_ok = validate_config_manifest(config_manifest, root=config_root)[0]
        upstream_file = root / "upstream.py"
        upstream_file.write_text("value = 1\n", encoding="utf-8")
        from freqtrade.hedge.r54.canonical import sha256_file

        upstream_ok = verify_upstream_files(
            root,
            (UpstreamFileExpectation("upstream.py", sha256_file(upstream_file)),),
        )[0]
        _record(checks, "19_config_and_upstream_governance", config_ok and upstream_ok)

        continue_summary = run_continue_all(
            (
                CommandSpec("pass", (sys.executable, "-c", "print('ok')")),
                CommandSpec("fail", (sys.executable, "-c", "raise SystemExit(2)")),
                CommandSpec("after", (sys.executable, "-c", "print('after')")),
            ),
            artifact_dir=root / "continue-all",
        )
        bundle_path = build_evidence_bundle(
            root / "bundle.zip",
            (envelope_path, config_manifest),
            account_id=account_id,
        )
        bundle_ok = verify_evidence_bundle(bundle_path)[0]
        _record(
            checks,
            "20_continue_all_and_evidence_bundle",
            not continue_summary.passed
            and len(continue_summary.results) == 3
            and continue_summary.results[2].passed
            and bundle_ok,
        )

        # Full cumulative promotion model: insert every required artifact as synthetic
        # PASS evidence, then verify the final stage is reachable only through all gates.
        recorded = {ArtifactKind.MAINNET_READONLY_PREFLIGHT: preflight_evidence}
        recorded.update(
            {
                ArtifactKind.MAINNET_READONLY_24H: evidence24b,
                ArtifactKind.MAINNET_READONLY_72H: evidence72,
                ArtifactKind.POSTGRES_MATRIX: pg,
                ArtifactKind.BACKUP_RESTORE_DRILL: backup,
                ArtifactKind.ALERT_DRILL: alert,
                ArtifactKind.INCIDENT_DRILL: incident_evidence,
                ArtifactKind.RESOURCE_SOAK: resources,
                ArtifactKind.RESTRICTED_AUTO_24H: auto24,
                ArtifactKind.RESTRICTED_AUTO_72H: auto72,
            }
        )
        for item in [*mainnet_evidence]:
            recorded[item.kind] = item
        for kind in canary_kinds:
            recorded[kind] = build_canary_evidence(
                kind=kind,
                result=canary,
                account_id=account_id,
                environment="TESTNET",
                max_allowed_notional=Decimal("10"),
                require_flat_finish=True,
            )
        for kind in {kind for policy in POLICIES.values() for kind in policy.required}:
            item = recorded.get(kind)
            if item is None:
                rule = EVIDENCE_RULES[kind]
                item = create_envelope(
                    kind=kind,
                    account_id=account_id,
                    environment=rule.environment,
                    producer="offline",
                    payload={"synthetic_offline_acceptance": True},
                    status=CheckStatus.PASS,
                    ttl_seconds=3600,
                    write_surface=next(iter(rule.write_surfaces)),
                    real_order_requests=rule.minimum_requests,
                )
            store.put_evidence(item)
        final_decision = R54PromotionEngine(store).evaluate(
            account_id=account_id,
            stage=R5Stage.EXPANDED_LIMITS,
        )

        passed = (
            all(item["status"] == "PASS" for item in checks)
            and final_decision.status is GateStatus.READY
        )
        result = {
            "schema_version": 1,
            "toolkit": "R5.4.0",
            "status": "PASS" if passed else "FAIL",
            "offline_only": True,
            "exchange_access": "NONE",
            "real_order_requests": 0,
            "check_count": len(checks),
            "passed_count": sum(item["status"] == "PASS" for item in checks),
            "failed_count": sum(item["status"] == "FAIL" for item in checks),
            "final_synthetic_promotion_status": final_decision.status.value,
            "checks": checks,
        }
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return result


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    result = run(output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
