from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from freqtrade.hedge.readonly.soak_monitor import SoakMonitor, SoakObservation
from freqtrade.hedge.r54.canary import CanaryResult, build_canary_evidence
from freqtrade.hedge.r54.models import ArtifactKind, CheckStatus
from freqtrade.hedge.r54.postgres import PostgresMatrixResult, build_postgres_evidence
from freqtrade.hedge.r54.soak import build_soak_evidence


def test_24h_soak_evidence_passes(tmp_path) -> None:
    path = tmp_path / "soak.jsonl"
    monitor = SoakMonitor(path, expected_interval_seconds=3600.0)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for hour in range(25):
        monitor.append(
            SoakObservation(
                observed_at=start + timedelta(hours=hour),
                service_state="READY",
                stream_age_seconds=1.0,
                fast_diff_count=0,
                full_diff_count=0,
                wallet_delta=Decimal("0"),
                funding_income=Decimal("0"),
                realized_pnl=Decimal("0"),
                commissions=Decimal("0"),
                transfers=Decimal("0"),
            )
        )
    evidence_24, evidence_72 = build_soak_evidence(
        observations_path=path,
        account_id="acct",
        expected_interval_seconds=3600.0,
    )
    assert evidence_24.status is CheckStatus.PASS
    assert evidence_72 is None


def test_postgres_matrix_requires_all_checks() -> None:
    checks = {
        "fresh_migration": True,
        "idempotent_upgrade": True,
        "upgrade_downgrade_upgrade": True,
        "schema_parity": True,
        "constraint_parity": True,
        "concurrent_writer_fencing": True,
        "outbox_recovery": True,
        "backup_restore": True,
    }
    result = PostgresMatrixResult(
        checks=checks,
        pytest_passed=50,
        pytest_failed=0,
        server_version="16.3",
        dsn_redacted="postgresql://user:***@localhost/db",
    )
    assert build_postgres_evidence(result, account_id="acct").status is CheckStatus.PASS


def test_canary_requires_terminal_reconciled_flat_finish() -> None:
    result = CanaryResult(
        request_count=1,
        acknowledged_count=1,
        terminal_count=1,
        unknown_count=0,
        max_notional=Decimal("5"),
        flat_finish=True,
        reconciliation_diff_count=0,
        external_order_count=0,
    )
    evidence = build_canary_evidence(
        kind=ArtifactKind.MAINNET_SUBMIT_CANCEL,
        result=result,
        account_id="acct",
        environment="MAINNET",
        max_allowed_notional=Decimal("10"),
        require_flat_finish=True,
    )
    assert evidence.status is CheckStatus.PASS
    assert evidence.real_order_requests == 1
