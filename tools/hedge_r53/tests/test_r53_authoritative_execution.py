from __future__ import annotations

import hashlib
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from freqtrade.hedge.concurrency.database_lease import InMemoryDatabaseLeaseStore
from freqtrade.hedge.concurrency.position_lock import PositionLockManager
from freqtrade.hedge.concurrency.single_writer import SingleWriterGuard
from freqtrade.hedge.execution.fake_exchange import FakeExchangeExecutionPort
from freqtrade.hedge.execution.service import IntentAction, OrderIntent, OrderType, PositionSide
from freqtrade.hedge.execution.state_machine import OrderState
from freqtrade.hedge.planning.context import PositionSide as PlannerSide
from freqtrade.hedge.readiness.checks import ReadinessInputs
from freqtrade.hedge.risk.commit import SqlRiskApprovalCommitStore
from freqtrade.hedge.risk.limits import RiskLimits
from freqtrade.hedge.risk.portfolio import PositionRiskLeg, build_risk_portfolio
from freqtrade.hedge.risk.runtime import build_hedge_risk_runtime
from freqtrade.persistence.hedge_models import create_hedge_tables

from freqtrade.hedge.r5.arming import R5StageArmAuthority
from freqtrade.hedge.r5.authoritative_runtime import build_r5_authoritative_runtime
from freqtrade.hedge.r5.budget import R5BudgetExceeded
from freqtrade.hedge.r5.emergency import R5EmergencyExitAuthority
from freqtrade.hedge.r5.fill_closed_loop import R5SqlFillProjector
from freqtrade.hedge.r5.fill_processor import R5InventoryProcessor
from freqtrade.hedge.r5.models import R5AccountSnapshot, R5Stage, default_limit_profile
from freqtrade.hedge.r5.sql_budget import SqlR5DailyBudgetStore
from freqtrade.hedge.r5.strategy_store import R5StrategyStateStore

D = Decimal


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_hedge_tables(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _writer():
    guard = SingleWriterGuard(
        InMemoryDatabaseLeaseStore(), owner_id="r53-test", clock_ms=lambda: 1000
    )
    guard.acquire()
    return guard


def _risk(writer):
    inputs = ReadinessInputs(
        database_migration_succeeded=True,
        single_writer_lease_valid=True,
        position_mode="hedge",
        margin_mode="cross",
        configured_leverage=D("1"),
        observed_leverages=(D("1"),),
        unmanaged_position_count=0,
        unmanaged_order_count=0,
        rest_snapshot_valid=True,
        user_stream_fresh=True,
        unknown_order_count=0,
        reconciliation_converged=True,
        risk_data_valid=True,
    )
    return build_hedge_risk_runtime(
        limits=RiskLimits(
            max_margin_utilization=D("0.9"),
            min_liquidation_buffer_ratio=D("0"),
            max_gross_notional=D("1000"),
            max_single_order_notional=D("100"),
        ),
        writer=writer,
        readiness_inputs=inputs,
        position_locks=PositionLockManager(),
        enable_lease_runner=False,
        readiness_clock_ms=lambda: 1000,
        reservation_clock_ms=lambda: 1000,
    )


def test_fresh_schema_contains_execution_and_r53_authority_tables():
    engine, _ = _database()
    names = set(inspect(engine).get_table_names())
    assert {
        "hedge_execution_order_states",
        "hedge_execution_idempotency",
        "hedge_r5_daily_budgets",
        "hedge_r5_budget_reservations",
        "hedge_r5_income_events",
    } <= names


def test_stage_permit_revoke_all_invalidates_held_object():
    stage = R5Stage.RESTRICTED_AUTO
    operator = "operator-secret"
    risk = "risk-secret"
    authority = R5StageArmAuthority(
        operator_hashes={stage: hashlib.sha256(operator.encode()).hexdigest()},
        risk_hashes={stage: hashlib.sha256(risk.encode()).hexdigest()},
    )
    permit = authority.issue(
        stage=stage,
        run_id="run-1",
        account_fingerprint="abc",
        actor="operator",
        operator_token=operator,
        risk_token=risk,
        confirmation="R5:RESTRICTED_AUTO:run-1:abc",
        profile=default_limit_profile(stage),
    )
    authority.revoke_all()
    with pytest.raises(PermissionError, match="revoked"):
        permit.preflight(1)


def test_sql_budget_lifecycle_and_income_are_idempotent():
    _, sessions = _database()
    budget = SqlR5DailyBudgetStore(sessions)
    profile = default_limit_profile(R5Stage.RESTRICTED_AUTO)
    current = R5AccountSnapshot("binance-usdm:test", D("1000"), D("0"), D("0"), 0)
    projected = R5AccountSnapshot("binance-usdm:test", D("1000"), D("10"), D("0"), 1)
    reservation = budget.prepare_orders(
        account_id=current.account_id,
        profile=profile,
        reservations=((D("10"), projected, current, False),),
    )
    assert reservation is not None and budget.snapshot(current.account_id).orders == 1
    assert budget.release_reservation(reservation.reservation_id).orders == 0
    reservation = budget.prepare_orders(
        account_id=current.account_id,
        profile=profile,
        reservations=((D("10"), projected, current, False),),
    )
    assert reservation is not None
    assert budget.confirm_reservation(reservation.reservation_id).turnover == D("10")
    with pytest.raises(RuntimeError):
        budget.release_reservation(reservation.reservation_id)
    event = dict(
        account_id=current.account_id,
        external_event_id="income-1",
        income_type="FUNDING_FEE",
        amount=D("-2"),
        occurred_at=datetime.now(UTC),
        profile=profile,
    )
    assert budget.record_income_event(**event)[0] is True
    assert budget.record_income_event(**event)[0] is False
    assert budget.authoritative_realized_loss(current.account_id) == D("2")


def test_sql_budget_checks_complete_batch_before_write():
    _, sessions = _database()
    budget = SqlR5DailyBudgetStore(sessions)
    profile = default_limit_profile(R5Stage.RESTRICTED_AUTO)
    account = "binance-usdm:test"
    current = R5AccountSnapshot(account, D("1000"), D("0"), D("0"), 0)
    too_large = R5AccountSnapshot(account, D("1000"), D("150"), D("0"), 1)
    with pytest.raises(R5BudgetExceeded):
        budget.prepare_orders(
            account_id=account,
            profile=profile,
            reservations=((D("25"), too_large, current, False),),
        )
    assert budget.snapshot(account).orders == 0


def test_authoritative_fake_submit_fill_sql_and_inventory_closed_loop():
    _, sessions = _database()
    writer = _writer()
    risk = _risk(writer)
    with tempfile.TemporaryDirectory() as directory:
        state = R5StrategyStateStore(Path(directory) / "state.sqlite")
        inventory = R5InventoryProcessor(state)
        budget = SqlR5DailyBudgetStore(sessions)
        projector = R5SqlFillProjector(
            session_factory=sessions,
            inventory=inventory,
            budget=budget,
            profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
        )
        fake = FakeExchangeExecutionPort()
        fake.queue_snapshot(
            OrderState.FILLED,
            filled_quantity="0.1",
            average_price="100",
            exchange_trade_id="trade-1",
        )
        runtime = build_r5_authoritative_runtime(
            account_id="binance-usdm:test",
            session_factory=sessions,
            writer=writer,
            position_locks=PositionLockManager(),
            risk_runtime=risk,
            risk_commit_store=SqlRiskApprovalCommitStore(sessions),
            exchange=fake,
            fill_projector=projector,
            budget=budget,
            leverage=D("1"),
            maintenance_margin_rate=D("0.01"),
        )
        portfolio = build_risk_portfolio(
            account_id="binance-usdm:test",
            equity=D("1000"),
            wallet_balance=D("1000"),
            available_balance=D("1000"),
            strict_completeness=False,
            observed_at_ms=1000,
        )
        intent = OrderIntent(
            account_id="binance-usdm:test",
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            action=IntentAction.OPEN,
            quantity=D("0.1"),
            idempotency_key="r53-fill-1",
            order_type=OrderType.LIMIT,
            limit_price=D("100"),
            metadata={"bucket": "CORE", "time_in_force": "GTC", "actor": "test"},
        )
        from freqtrade.hedge.r5.arming import R5StagePermit
        permit = R5StagePermit(
            R5Stage.RESTRICTED_AUTO,
            "run",
            "actor",
            "permit",
            time.monotonic() + 300,
            10,
        )
        report = runtime.execute_batch(
            portfolio=portfolio,
            intents=(intent,),
            permit=permit,
            profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
            budget=budget,
            budget_reservations=((
                D("10"),
                R5AccountSnapshot("binance-usdm:test", D("1000"), D("10"), D("0"), 1),
                R5AccountSnapshot("binance-usdm:test", D("1000"), D("0"), D("0"), 0),
                False,
            ),),
        )
        assert report.writes_attempted == 1
        assert report.fill_projection.applied == 1
        leg = state.inventory_leg(
            account_id="binance-usdm:test",
            symbol="ETHUSDT",
            side=PlannerSide.LONG,
        )
        assert leg is not None and leg.core_quantity == D("0.1")
        assert budget.snapshot("binance-usdm:test").orders == 1


def test_emergency_permit_is_exact_scope_single_use_and_revocable():
    operator = "op"
    risk = "risk"
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
        reason="risk exit",
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
        idempotency_key="emergency-1",
        order_type=OrderType.MARKET,
        reduce_only=True,
        metadata={"reference_price": "100"},
    )
    permit.authorize(intent)
    with pytest.raises(PermissionError, match="consumed"):
        permit.authorize(intent)


def test_r5_authority_migration_create_and_verify(tmp_path):
    from freqtrade.persistence.hedge_migrations import (
        _step_create_r5_authority_tables,
        _step_verify_r5_authority_tables,
        migration_plan_ids,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    created = _step_create_r5_authority_tables(engine)
    verified = _step_verify_r5_authority_tables(engine)
    assert set(created["tables"]) == {
        "hedge_r5_daily_budgets",
        "hedge_r5_budget_reservations",
        "hedge_r5_income_events",
    }
    assert verified["verified"] is True
    assert migration_plan_ids()[-2:] == (
        "H3-033-r5-authority-ledger",
        "H3-034-verify-r5-authority-ledger",
    )


def test_decision_cursor_two_phase_lease_and_crash_recovery(tmp_path):
    store = R5StrategyStateStore(tmp_path / "cursor.sqlite")
    close_at = datetime.fromtimestamp(1800, tz=UTC)
    assert store.begin_decision_cursor(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        timeframe="1m",
        candle_close=close_at,
        decision_id="decision-1",
        lease_owner="worker-a",
        lease_seconds=1,
    ) is True
    assert store.begin_decision_cursor(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        timeframe="1m",
        candle_close=close_at,
        decision_id="decision-1",
        lease_owner="worker-b",
        lease_seconds=1,
    ) is False
    store.release_decision_cursor(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        timeframe="1m",
        decision_id="decision-1",
        lease_owner="worker-a",
    )
    assert store.begin_decision_cursor(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        timeframe="1m",
        candle_close=close_at,
        decision_id="decision-1",
        lease_owner="worker-b",
        lease_seconds=1,
    ) is True
    store.complete_decision_cursor(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        timeframe="1m",
        decision_id="decision-1",
        lease_owner="worker-b",
    )
    assert store.decision_cursor(
        account_id="binance-usdm:test", symbol="ETHUSDT", timeframe="1m"
    )["status"] == "COMPLETED"


def test_cross_process_shared_weight_budget_coordinates_instances(tmp_path):
    from freqtrade.hedge.exchange.shared_rate_limit import SqliteSharedWeightBudget

    now = lambda: 120.0
    first = SqliteSharedWeightBudget(
        tmp_path / "weight.sqlite",
        limit_per_minute=100,
        reserve_weight=10,
        now_epoch=now,
    )
    second = SqliteSharedWeightBudget(
        tmp_path / "weight.sqlite",
        limit_per_minute=100,
        reserve_weight=10,
        now_epoch=now,
    )
    assert first.reserve_weight(60).granted is True
    denied = second.reserve_weight(31)
    assert denied.granted is False
    assert denied.used_weight == 60
    assert second.reserve_weight(30).granted is True


def test_authoritative_emergency_reduce_uses_same_sql_closed_loop(tmp_path):
    _, sessions = _database()
    writer = _writer()
    risk_runtime = _risk(writer)
    state = R5StrategyStateStore(tmp_path / "state.sqlite")
    budget = SqlR5DailyBudgetStore(sessions)
    projector = R5SqlFillProjector(
        session_factory=sessions,
        inventory=R5InventoryProcessor(state),
        budget=budget,
        profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
    )
    fake = FakeExchangeExecutionPort()
    fake.queue_snapshot(
        OrderState.FILLED,
        filled_quantity="0.1",
        average_price="100",
        exchange_trade_id="emergency-trade-1",
    )
    runtime = build_r5_authoritative_runtime(
        account_id="binance-usdm:test",
        session_factory=sessions,
        writer=writer,
        position_locks=PositionLockManager(),
        risk_runtime=risk_runtime,
        risk_commit_store=SqlRiskApprovalCommitStore(sessions),
        exchange=fake,
        fill_projector=projector,
        budget=budget,
        leverage=D("1"),
        maintenance_margin_rate=D("0.01"),
    )
    portfolio = build_risk_portfolio(
        account_id="binance-usdm:test",
        equity=D("1000"),
        wallet_balance=D("1000"),
        available_balance=D("1000"),
        strict_completeness=False,
        observed_at_ms=1000,
        positions=(
            PositionRiskLeg(
                account_id="binance-usdm:test",
                symbol="ETHUSDT",
                position_side="LONG",
                quantity=D("1"),
                mark_price=D("100"),
                leverage=D("1"),
                maintenance_margin=D("1"),
                liquidation_price=D("50"),
            ),
        ),
    )
    runtime.seed_position_projection(
        symbol="ETHUSDT",
        position_side="LONG",
        quantity=D("1"),
        average_entry_price=D("100"),
        observed_at=datetime.now(UTC),
    )
    state.adopt_core_baseline(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        long_quantity=D("1"),
        long_average_price=D("100"),
        short_quantity=D("0"),
        short_average_price=D("0"),
        manifest_sha256="test-emergency-adoption",
    )
    intent = OrderIntent(
        account_id="binance-usdm:test",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        action=IntentAction.REDUCE,
        quantity=D("0.1"),
        idempotency_key="emergency-runtime-1",
        order_type=OrderType.MARKET,
        reduce_only=True,
        metadata={"reference_price": "100", "bucket": "CORE"},
    )
    operator = "emergency-op"
    risk = "emergency-risk"
    authority = R5EmergencyExitAuthority(
        operator_sha256=hashlib.sha256(operator.encode()).hexdigest(),
        risk_sha256=hashlib.sha256(risk.encode()).hexdigest(),
    )
    emergency = authority.issue(
        account_id=intent.account_id,
        symbol=intent.symbol,
        position_side=intent.position_side,
        max_quantity=D("0.1"),
        max_notional=D("10"),
        reason="liquidation protection",
        operator_token=operator,
        risk_token=risk,
        confirmation="EMERGENCY_CLOSE:binance-usdm:test:ETHUSDT:LONG:0.1",
    )
    from freqtrade.hedge.r5.arming import R5StagePermit

    stage_permit = R5StagePermit(
        R5Stage.RESTRICTED_AUTO,
        "run-emergency",
        "operator",
        "permit-emergency",
        time.monotonic() + 60,
        2,
    )
    current = R5AccountSnapshot("binance-usdm:test", D("1000"), D("100"), D("0"), 0)
    projected = R5AccountSnapshot("binance-usdm:test", D("1000"), D("90"), D("0"), 1)
    report = runtime.execute_emergency_reduce(
        portfolio=portfolio,
        intent=intent,
        emergency_permit=emergency,
        stage_permit=stage_permit,
        profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
        budget=budget,
        budget_reservations=((D("10"), projected, current, True),),
    )
    assert report.writes_attempted == 1
    assert report.fill_projection.applied == 1
    assert report.submissions[0].order.lifecycle.status is OrderState.FILLED
