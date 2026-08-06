from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from freqtrade.hedge.r5.arming import R5StageArmAuthority
from freqtrade.hedge.r5.emergency import R5EmergencyExitAuthority
from freqtrade.hedge.r5.income_closed_loop import R5IncomeProjector
from freqtrade.hedge.r5.models import R5Stage, default_limit_profile
from freqtrade.hedge.r5.production_session import (
    R5ClosedCandle,
    R5ClosedCandleProductionSession,
)
from freqtrade.hedge.r5.sql_budget import SqlR5DailyBudgetStore
from freqtrade.hedge.r5.user_stream_evidence import R5UserStreamEvidence
from freqtrade.hedge.execution.service import IntentAction, OrderIntent, OrderType, PositionSide
from freqtrade.persistence.hedge_models import create_hedge_tables

D = Decimal


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_hedge_tables(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _evidence_files(tmp_path: Path, *, stream_connected: bool = True) -> tuple[Path, Path, Path]:
    fingerprint = "a" * 64
    now = datetime.now(UTC).isoformat()
    r4 = tmp_path / "r4.json"
    r24 = tmp_path / "r24.json"
    stream = tmp_path / "stream.json"
    _write_json(
        r4,
        {
            "status": "R4_FULL_COMPLETION_COMPLETE_MAINNET_LOCKED",
            "mainnet_live_exchange_write": "LOCKED",
            "real_mainnet_order_requests_sent": 0,
        },
    )
    _write_json(
        r24,
        {
            "status": "FULL_PASS",
            "security": {"exchange_writes": "LOCKED"},
            "account_fingerprint": fingerprint,
        },
    )
    _write_json(
        stream,
        {
            "status": "FULL_PASS",
            "created_at_utc": now,
            "result": {"final_state": "READY", "ready_observed": True},
            "direction2_health": {
                "clock_synchronized": True,
                "configuration_valid": True,
                "reconciliation_consistent": True,
                "rest_fresh": True,
                "stream_connected": stream_connected,
                "stream_fresh": True,
                "gap_detected": False,
                "unmanaged_order_count": 0,
                "unmanaged_position_count": 0,
                "reason_codes": [],
            },
            "security": {
                "exchange_order_writes": "LOCKED",
                "trading_endpoints_allowed": False,
                "credentials_persisted": False,
                "write_lock": {"enabled": True},
            },
            "database": {
                "reopen_readback": "PASS",
                "integrity_before_reopen": {"status": "PASS"},
                "integrity_after_reopen": {"status": "PASS"},
            },
            "scope": {
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "account_fingerprint": fingerprint,
            },
        },
    )
    return r4, r24, stream


def test_user_stream_evidence_requires_real_fresh_health(tmp_path: Path) -> None:
    r4, r24, stream = _evidence_files(tmp_path)
    evidence = R5UserStreamEvidence.load(
        r4_report=r4,
        r24_report=r24,
        user_stream_report=stream,
        max_age_seconds=300,
    )
    assert evidence.stream_connected is True
    assert evidence.stream_fresh is True
    assert evidence.reconciliation_converged is True
    assert evidence.gap_detected is False



def test_user_stream_evidence_rejects_disconnected_stream(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    r4, r24, stream = _evidence_files(bad, stream_connected=False)
    with pytest.raises(PermissionError, match="health check failed|not write-ready"):
        R5UserStreamEvidence.load(
            r4_report=r4,
            r24_report=r24,
            user_stream_report=stream,
            max_age_seconds=300,
        )


def test_stage_permit_concurrent_consume_is_atomic() -> None:
    stage = R5Stage.RESTRICTED_AUTO
    operator = "operator-token"
    risk = "risk-token"
    authority = R5StageArmAuthority(
        operator_hashes={stage: hashlib.sha256(operator.encode()).hexdigest()},
        risk_hashes={stage: hashlib.sha256(risk.encode()).hexdigest()},
    )
    profile = default_limit_profile(stage)
    permit = authority.issue(
        stage=stage,
        run_id="atomic-run",
        account_fingerprint="f" * 64,
        actor="test",
        operator_token=operator,
        risk_token=risk,
        confirmation=f"R5:{stage.value}:atomic-run:{'f' * 64}",
        profile=profile,
    )

    def attempt() -> bool:
        try:
            permit.consume("ATOMIC_TEST")
            return True
        except PermissionError:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = tuple(pool.map(lambda _: attempt(), range(profile.write_request_budget + 30)))
    assert sum(results) == profile.write_request_budget
    assert permit.remaining_writes == 0


def test_emergency_permit_concurrent_authorize_is_single_use() -> None:
    operator = "emergency-op"
    risk = "emergency-risk"
    authority = R5EmergencyExitAuthority(
        operator_sha256=hashlib.sha256(operator.encode()).hexdigest(),
        risk_sha256=hashlib.sha256(risk.encode()).hexdigest(),
    )
    permit = authority.issue(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        max_quantity=D("0.1"),
        max_notional=D("20"),
        reason="test",
        operator_token=operator,
        risk_token=risk,
        confirmation="EMERGENCY_CLOSE:binance-usdm:test:ETHUSDT:LONG:0.1",
    )
    intent = OrderIntent(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        action=IntentAction.REDUCE,
        quantity=D("0.1"),
        idempotency_key="emergency-atomic",
        order_type=OrderType.MARKET,
        reduce_only=True,
        metadata={"reference_price": "100"},
    )

    def attempt() -> bool:
        try:
            permit.authorize(intent)
            return True
        except PermissionError:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = tuple(pool.map(lambda _: attempt(), range(32)))
    assert sum(results) == 1


def test_income_is_single_financial_daily_loss_authority() -> None:
    engine, sessions = _database()
    budget = SqlR5DailyBudgetStore(sessions)
    profile = default_limit_profile(R5Stage.RESTRICTED_AUTO)
    account = "binance-usdm:test"

    # Inventory/fill projection no longer writes financial daily loss.
    assert budget.authoritative_realized_loss(account) == D("0")
    income = R5IncomeProjector(budget=budget, profile=profile)
    row = {
        "tranId": "realized-trade-1",
        "incomeType": "REALIZED_PNL",
        "income": "-1",
        "time": int(datetime.now(UTC).timestamp() * 1000),
        "symbol": "ETHUSDT",
        "asset": "USDT",
    }
    first = income.project(account_id=account, rows=(row,))
    replay = income.project(account_id=account, rows=(row,))
    assert first.applied == 1 and first.realized_loss == D("1")
    assert replay.replayed == 1 and replay.realized_loss == D("1")
    engine.dispose()


class _CursorStore:
    def __init__(self) -> None:
        self.status = None

    def begin_decision_cursor(self, **kwargs) -> bool:
        del kwargs
        self.status = "IN_FLIGHT"
        return True

    def complete_decision_cursor(self, **kwargs) -> None:
        del kwargs
        self.status = "COMPLETED"

    def release_decision_cursor(self, **kwargs) -> None:
        del kwargs
        self.status = "RELEASED"


class _Budget:
    def snapshot(self, account_id):
        del account_id
        return type("Snapshot", (), {"realized_loss": D("0")})()

    def record_realized_pnl(self, **kwargs):
        del kwargs


def test_decision_cursor_is_not_completed_when_post_validation_fails() -> None:
    store = _CursorStore()
    account = "binance-usdm:test"
    snapshots = iter(
        (
            type("Snapshot", (), {
                "account_id": account,
                "unmanaged_positions": (),
                "open_orders": 0,
            })(),
            type("Snapshot", (), {
                "account_id": account,
                "unmanaged_positions": ("MANUAL",),
                "open_orders": 0,
            })(),
        )
    )
    stage = R5Stage.RESTRICTED_AUTO
    operator = "op"
    risk = "risk"
    authority = R5StageArmAuthority(
        operator_hashes={stage: hashlib.sha256(operator.encode()).hexdigest()},
        risk_hashes={stage: hashlib.sha256(risk.encode()).hexdigest()},
    )
    permit = authority.issue(
        stage=stage,
        run_id="cursor-run",
        account_fingerprint="c" * 64,
        actor="test",
        operator_token=operator,
        risk_token=risk,
        confirmation=f"R5:{stage.value}:cursor-run:{'c' * 64}",
        profile=default_limit_profile(stage),
    )
    candle = R5ClosedCandle(datetime.now(UTC), "decision-1")
    provided = False

    def candle_provider():
        nonlocal provided
        if provided:
            return None
        provided = True
        return candle

    session = R5ClosedCandleProductionSession(
        account_id=account,
        symbol="ETHUSDT",
        profile=default_limit_profile(stage),
        store=store,
        budget=_Budget(),
        permit=permit,
        authority=authority,
        snapshot_provider=lambda: next(snapshots),
        closed_candle_provider=candle_provider,
        cycle=lambda _candle, _snapshot: {"writes_attempted": 0},
        realized_pnl_provider=lambda: D("0"),
        clock_refresh=lambda: None,
        sleep=lambda _seconds: None,
        monotonic=time.monotonic,
    )
    with pytest.raises(PermissionError, match="unmanaged positions"):
        session.run(max_cycles=1)
    assert store.status == "RELEASED"


def test_production_session_does_not_write_inventory_pnl_into_daily_loss() -> None:
    class StrictBudget(_Budget):
        def record_realized_pnl(self, **kwargs):
            raise AssertionError("inventory PnL must not mutate authoritative daily loss")

    store = _CursorStore()
    account = "binance-usdm:test"
    safe = type(
        "Snapshot",
        (),
        {"account_id": account, "unmanaged_positions": (), "open_orders": 0},
    )()
    stage = R5Stage.RESTRICTED_AUTO
    operator = "op-success"
    risk = "risk-success"
    authority = R5StageArmAuthority(
        operator_hashes={stage: hashlib.sha256(operator.encode()).hexdigest()},
        risk_hashes={stage: hashlib.sha256(risk.encode()).hexdigest()},
    )
    permit = authority.issue(
        stage=stage,
        run_id="success-run",
        account_fingerprint="d" * 64,
        actor="test",
        operator_token=operator,
        risk_token=risk,
        confirmation=f"R5:{stage.value}:success-run:{'d' * 64}",
        profile=default_limit_profile(stage),
    )
    candle = R5ClosedCandle(datetime.now(UTC), "decision-success")
    session = R5ClosedCandleProductionSession(
        account_id=account,
        symbol="ETHUSDT",
        profile=default_limit_profile(stage),
        store=store,
        budget=StrictBudget(),
        permit=permit,
        authority=authority,
        snapshot_provider=lambda: safe,
        closed_candle_provider=lambda: candle,
        cycle=lambda _candle, _snapshot: {"writes_attempted": 0},
        realized_pnl_provider=lambda: D("-5"),
        clock_refresh=lambda: None,
        sleep=lambda _seconds: None,
        monotonic=time.monotonic,
    )
    report = session.run(max_cycles=1)
    assert report.cycles_completed == 1
    assert store.status == "COMPLETED"
