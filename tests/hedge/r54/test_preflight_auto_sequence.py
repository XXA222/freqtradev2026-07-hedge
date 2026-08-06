from __future__ import annotations

from decimal import Decimal

from freqtrade.hedge.r5.models import R5Stage
from freqtrade.hedge.r54.auto import AutoObservationResult, build_auto_observation_evidence
from freqtrade.hedge.r54.evidence import create_envelope
from freqtrade.hedge.r54.models import ArtifactKind, CheckStatus, GateStatus
from freqtrade.hedge.r54.preflight import (
    MainnetReadonlyPreflightResult,
    build_mainnet_preflight_evidence,
)
from freqtrade.hedge.r54.promotion import POLICIES, R54PromotionEngine
from freqtrade.hedge.r54.store import R54OperationalStore


def test_mainnet_preflight_requires_real_stream_cross_clock_and_restart() -> None:
    result = MainnetReadonlyPreflightResult(
        private_api_ok=True,
        hedge_mode=True,
        cross_symbols=("BTCUSDT", "ETHUSDT"),
        required_cross_symbols=("BTCUSDT", "ETHUSDT"),
        clock_offset_ms=12,
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
    assert build_mainnet_preflight_evidence(result, account_id="acct").status is CheckStatus.PASS


def test_auto_observation_requires_clean_24h() -> None:
    result = AutoObservationResult(
        duration_hours=24,
        cycle_count=1440,
        write_request_count=10,
        unknown_order_count=0,
        non_ready_cycle_count=0,
        reconciliation_diff_count=0,
        budget_breach_count=0,
        alert_count=1,
        uncontained_incident_count=0,
        realized_loss=Decimal("1"),
        maximum_drawdown=Decimal("2"),
        limit_realized_loss=Decimal("5"),
        limit_maximum_drawdown=Decimal("10"),
    )
    assert build_auto_observation_evidence(
        result, account_id="acct", minimum_hours=24
    ).status is CheckStatus.PASS


def test_later_stage_cannot_bypass_earlier_evidence(tmp_path) -> None:
    store = R54OperationalStore(tmp_path / "r54.sqlite")
    for kind in POLICIES[R5Stage.EXPANDED_LIMITS].required:
        store.put_evidence(
            create_envelope(
                kind=kind,
                account_id="acct",
                environment="MAINNET",
                producer="test",
                payload={"kind": kind.value},
                status=CheckStatus.PASS,
                write_surface="LIMITED_WRITE",
                real_order_requests=1,
            )
        )
    decision = R54PromotionEngine(store).evaluate(
        account_id="acct", stage=R5Stage.EXPANDED_LIMITS
    )
    assert decision.status is GateStatus.PENDING_EXTERNAL_EVIDENCE
    assert any(item.status is CheckStatus.PENDING for item in decision.checks)
