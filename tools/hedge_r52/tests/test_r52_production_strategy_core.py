from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from freqtrade.hedge.execution.service import IntentAction as ExecAction
from freqtrade.hedge.execution.state_machine import OrderState
from freqtrade.hedge.planning.context import (
    ActiveOrder,
    IntentAction,
    MarketSnapshot,
    OrderSide,
    OrderType,
    PlannerConfig,
    PositionBucket,
    PositionSide,
    TimeInForce,
)
from freqtrade.hedge.r5.mainnet_api import R5LivePreflight
from freqtrade.hedge.r5.market import R5Book, R5SymbolRules
from freqtrade.hedge.r5.models import R5AccountSnapshot, R5Stage, default_limit_profile
from freqtrade.hedge.r5.position_ledger import R5CoreTacticalLedger
from freqtrade.hedge.r5.strategy_runtime import (
    R5PositionPolicy,
    R5ProductionStrategyRuntime,
    R5StrategyFacts,
    R5StrategyMode,
    R5StrategySignals,
)
from freqtrade.hedge.r5.strategy_shadow import facts_from_preflight
from freqtrade.hedge.r5.strategy_store import R5StrategyStateStore


D = Decimal
ACCOUNT = "binance-usdm:test-account"


def planner_config() -> PlannerConfig:
    return PlannerConfig(
        core_wallet_exposure_long=D("0.03"),
        core_wallet_exposure_short=D("0.03"),
        tactical_wallet_exposure_long=D("0.02"),
        tactical_wallet_exposure_short=D("0.02"),
        max_wallet_exposure_long=D("0.08"),
        max_wallet_exposure_short=D("0.08"),
        max_gross_wallet_exposure=D("0.10"),
        initial_entry_fraction=D("0.5"),
        max_grid_layers=2,
        max_pending_entries=2,
        max_single_order_notional=D("25"),
        grid_spacing=D("0.01"),
        take_profit_layers=2,
        cooldown_seconds=0,
        trailing_trigger_distance=D("0"),
    )


def facts(*, now: datetime | None = None, long=D("0"), short=D("0"), external=0, managed=()):
    ts = now or datetime.now(UTC)
    return R5StrategyFacts(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        observed_at=ts,
        market=MarketSnapshot(
            "ETHUSDT", ts, D("99"), D("101"), D("100"),
            tick_size=D("0.1"), qty_step=D("0.01"), min_qty=D("0.01"), min_notional=D("5"),
        ),
        balance=D("1000"),
        equity=D("1000"),
        available_balance=D("1000"),
        leverage=D("10"),
        long_quantity=long,
        long_average_price=D("100") if long else D("0"),
        short_quantity=short,
        short_average_price=D("100") if short else D("0"),
        managed_orders=tuple(managed),
        external_order_count=external,
    )


class FakeExecutor:
    def __init__(self, *, unknown_submit: bool = False) -> None:
        self.submits = []
        self.cancels = []
        self.unknown_submit = unknown_submit

    @staticmethod
    def _result(intent, client_id: str, state: OrderState):
        lifecycle = SimpleNamespace(status=state, filled_quantity=D("0"))
        order = SimpleNamespace(intent=intent, client_order_id=client_id, lifecycle=lifecycle)
        return SimpleNamespace(order=order)

    def submit(self, intent):
        self.submits.append(intent)
        state = OrderState.UNKNOWN if self.unknown_submit else OrderState.ACKNOWLEDGED
        return self._result(intent, f"FTH-{len(self.submits):026d}", state)

    def cancel(self, client_order_id):
        self.cancels.append(client_order_id)
        return self._result(None, client_order_id, OrderState.CANCELED)

    def telemetry(self):
        return SimpleNamespace(write_requests=len(self.submits) + len(self.cancels))


def runtime(tmp_path: Path, *, mode=R5StrategyMode.SHADOW, executor=None, policy=R5PositionPolicy.PROTECT_AS_CORE):
    return R5ProductionStrategyRuntime(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        mode=mode,
        profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
        planner_config=planner_config(),
        store=R5StrategyStateStore(tmp_path / "strategy.sqlite"),
        executor=executor,
        position_policy=policy,
    )


def test_shadow_cycle_runs_full_dual_leg_planner_without_writes(tmp_path):
    subject = runtime(tmp_path)
    result = subject.run_cycle(
        cycle_id="cycle-shadow-1",
        facts=facts(),
        signals=R5StrategySignals(long_signal=D("1"), short_signal=D("1")),
    )
    assert result.status == "PASS"
    assert result.writes_attempted == 0
    assert result.submit_count > 0
    assert {item["position_side"] for item in result.report["planner"]["ideal_orders"]} == {"LONG", "SHORT"}
    assert result.report["exchange_write"] == "LOCKED"


def test_cycle_id_reuse_with_different_inputs_is_rejected(tmp_path):
    subject = runtime(tmp_path)
    first = subject.run_cycle(cycle_id="cycle-replay", facts=facts(), signals=R5StrategySignals())
    assert first.plan_sha256 != ""
    with pytest.raises(RuntimeError, match="different inputs"):
        subject.run_cycle(cycle_id="cycle-replay", facts=facts(long=D("1")), signals=R5StrategySignals())


def test_exact_cycle_replay_returns_durable_report(tmp_path):
    subject = runtime(tmp_path)
    snapshot = facts()
    first = subject.run_cycle(cycle_id="cycle-exact", facts=snapshot, signals=R5StrategySignals())
    second = subject.run_cycle(cycle_id="cycle-exact", facts=snapshot, signals=R5StrategySignals())
    assert second.report["canonical_payload_sha256"] == first.report["canonical_payload_sha256"]


def test_existing_unadopted_positions_are_protected_as_core_in_shadow(tmp_path):
    subject = runtime(tmp_path)
    result = subject.run_cycle(
        cycle_id="cycle-existing",
        facts=facts(long=D("2"), short=D("2"), external=5),
        signals=R5StrategySignals(),
    )
    assert "LONG:UNADOPTED_POSITION_PROTECTED_AS_CORE" in result.diagnostics
    assert "SHORT:UNADOPTED_POSITION_PROTECTED_AS_CORE" in result.diagnostics
    assert result.writes_attempted == 0
    assert result.external_order_count == 5


def test_mainnet_locked_high_gross_unstuck_is_aggressive_limit_and_notional_capped(tmp_path):
    subject = runtime(tmp_path, mode=R5StrategyMode.MAINNET_LOCKED)
    result = subject.run_cycle(
        cycle_id="cycle-real-account-gross",
        facts=facts(long=D("10"), short=D("10"), external=5),
        signals=R5StrategySignals(),
    )
    ideal = result.report["planner"]["ideal_orders"]
    unstuck = [item for item in ideal if item["action"] == "UNSTUCK"]
    assert {item["position_side"] for item in unstuck} == {"LONG", "SHORT"}
    assert all(item["order_type"] == OrderType.LIMIT.value for item in unstuck)
    assert all(item["time_in_force"] == TimeInForce.GTC.value for item in unstuck)
    assert all(D(item["notional"]) <= D("25") for item in unstuck)
    assert result.writes_attempted == 0
    assert result.report["mainnet_order_requests_sent"] == 0


def test_require_adoption_blocks_nonflat_unadopted_position(tmp_path):
    subject = runtime(tmp_path, policy=R5PositionPolicy.REQUIRE_ADOPTION)
    with pytest.raises(PermissionError, match="requires explicit adoption"):
        subject.run_cycle(cycle_id="cycle-adopt", facts=facts(long=D("1")), signals=R5StrategySignals())


def test_adopted_core_baseline_allows_tactical_inventory(tmp_path):
    subject = runtime(tmp_path, policy=R5PositionPolicy.REQUIRE_ADOPTION)
    subject.store.adopt_core_baseline(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        long_quantity=D("1"),
        long_average_price=D("90"),
        short_quantity=D("1"),
        short_average_price=D("110"),
        manifest_sha256="a" * 64,
    )
    result = subject.run_cycle(
        cycle_id="cycle-adopted",
        facts=facts(long=D("1.5"), short=D("1.5")),
        signals=R5StrategySignals(),
    )
    assert result.status == "PASS"


def test_stale_and_future_facts_fail_closed(tmp_path):
    subject = runtime(tmp_path)
    with pytest.raises(PermissionError, match="stale"):
        subject.run_cycle(cycle_id="cycle-stale", facts=facts(now=datetime.now(UTC) - timedelta(minutes=2)), signals=R5StrategySignals())
    with pytest.raises(PermissionError, match="future"):
        subject.run_cycle(cycle_id="cycle-future", facts=facts(now=datetime.now(UTC) + timedelta(seconds=10)), signals=R5StrategySignals())


def test_live_mode_rejects_external_orders_before_any_write(tmp_path):
    executor = FakeExecutor()
    subject = runtime(
        tmp_path,
        mode=R5StrategyMode.TESTNET,
        executor=executor,
        policy=R5PositionPolicy.REQUIRE_ADOPTION,
    )
    with pytest.raises(PermissionError, match="external active orders"):
        subject.run_cycle(cycle_id="cycle-external", facts=facts(external=1), signals=R5StrategySignals(long_signal=D("1")))
    assert executor.submits == [] and executor.cancels == []


def test_live_mode_submits_limit_orders_and_reduce_only_before_entries(tmp_path):
    executor = FakeExecutor()
    subject = runtime(
        tmp_path,
        mode=R5StrategyMode.TESTNET,
        executor=executor,
        policy=R5PositionPolicy.REQUIRE_ADOPTION,
    )
    result = subject.run_cycle(
        cycle_id="cycle-live",
        facts=facts(),
        signals=R5StrategySignals(long_signal=D("1"), short_signal=D("1")),
    )
    assert result.writes_attempted == len(executor.submits)
    assert executor.submits
    assert all(intent.order_type.value == "LIMIT" for intent in executor.submits)
    assert all(intent.metadata["time_in_force"] == "GTX" for intent in executor.submits if not intent.reduce_only)


def test_unknown_submit_marks_cycle_recovery_required(tmp_path):
    executor = FakeExecutor(unknown_submit=True)
    subject = runtime(
        tmp_path,
        mode=R5StrategyMode.TESTNET,
        executor=executor,
        policy=R5PositionPolicy.REQUIRE_ADOPTION,
    )
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        subject.run_cycle(cycle_id="cycle-unknown", facts=facts(), signals=R5StrategySignals(long_signal=D("1")))
    assert subject.store.cycle("cycle-unknown")["status"] == "RECOVERY_REQUIRED"


def test_runtime_refuses_nonwrite_mode_executor_and_write_mode_without_executor(tmp_path):
    with pytest.raises(ValueError, match="must not receive"):
        runtime(tmp_path, executor=FakeExecutor())
    with pytest.raises(ValueError, match="requires an executor"):
        runtime(tmp_path, mode=R5StrategyMode.TESTNET)


def test_store_requires_plan_before_execution_and_persists_leg_state(tmp_path):
    store = R5StrategyStateStore(tmp_path / "store.sqlite")
    store.claim_cycle(cycle_id="c", account_id=ACCOUNT, symbol="ETHUSDT", mode="SHADOW", input_payload={"x": 1})
    with pytest.raises(RuntimeError):
        store.begin_execution("c")
    store.persist_plan(cycle_id="c", plan={"orders": []})
    store.begin_execution("c")
    long_state, short_state = store.load_leg_states(account_id=ACCOUNT, symbol="ETHUSDT")
    store.complete_cycle(cycle_id="c", report={"ok": True}, long_state=long_state.next_sequence(), short_state=short_state.next_sequence())
    reloaded = R5StrategyStateStore(tmp_path / "store.sqlite")
    next_long, next_short = reloaded.load_leg_states(account_id=ACCOUNT, symbol="ETHUSDT")
    assert next_long.sequence == 1 and next_short.sequence == 1
    assert reloaded.integrity()["status"] == "PASS"


def test_core_tactical_ledger_tracks_long_tactical_profit_and_core_protection():
    leg = R5CoreTacticalLedger.adopt_as_core(side=PositionSide.LONG, quantity=D("1"), average_price=D("100"))
    leg = R5CoreTacticalLedger.open_fill(
        leg, bucket=PositionBucket.TACTICAL, quantity=D("1"), price=D("90"), tactical_lot_id="lot-1"
    )
    reduced = R5CoreTacticalLedger.reduce_fill(
        leg, bucket=PositionBucket.TACTICAL, quantity=D("1"), price=D("110")
    )
    assert reduced.core_quantity == D("1")
    assert reduced.tactical_quantity == D("0")
    assert reduced.tactical_realized_pnl == D("20")
    with pytest.raises(PermissionError, match="core protection"):
        R5CoreTacticalLedger.reduce_fill(
            reduced,
            bucket=PositionBucket.CORE,
            quantity=D("0.5"),
            price=D("110"),
            protected_core_quantity=D("1"),
        )


def test_core_tactical_ledger_tracks_short_profit_and_funding():
    leg = R5CoreTacticalLedger.adopt_as_core(side=PositionSide.SHORT, quantity=D("0"), average_price=D("0"))
    leg = R5CoreTacticalLedger.open_fill(
        leg, bucket=PositionBucket.TACTICAL, quantity=D("2"), price=D("100"), tactical_lot_id="short-lot"
    )
    leg = R5CoreTacticalLedger.allocate_funding(leg, D("-2"))
    reduced = R5CoreTacticalLedger.reduce_fill(
        leg, bucket=PositionBucket.TACTICAL, quantity=D("1"), price=D("90"), tactical_lot_id="short-lot"
    )
    assert reduced.tactical_realized_pnl == D("10")
    assert reduced.tactical_lots[0].funding == D("-2")


def test_shadow_adapter_classifies_unknown_exchange_orders_as_external(tmp_path):
    store = R5StrategyStateStore(tmp_path / "shadow.sqlite")
    now = datetime.now(UTC)
    preflight = R5LivePreflight(
        readonly_status="FULL_PASS",
        hedge_mode_enabled=True,
        single_asset_mode=True,
        clock_offset_ms=0,
        futures_trading_permission=False,
        ip_restricted=False,
        withdrawals_disabled=True,
        cross_margin_symbols=("BTCUSDT", "ETHUSDT"),
        account=R5AccountSnapshot(ACCOUNT, D("1000"), D("100"), D("100"), 1, (), now),
        rules={
            "ETHUSDT": R5SymbolRules("ETHUSDT", D("0.1"), D("0.01"), D("0.01"), D("1000"), D("5")),
            "BTCUSDT": R5SymbolRules("BTCUSDT", D("0.1"), D("0.001"), D("0.001"), D("100"), D("5")),
        },
        books={
            "ETHUSDT": R5Book("ETHUSDT", D("99"), D("101"), D("100")),
            "BTCUSDT": R5Book("BTCUSDT", D("999"), D("1001"), D("1000")),
        },
        request_count=12,
        managed_position_legs=(
            {"symbol": "ETHUSDT", "position_side": "LONG", "position_amount": "1", "entry_price": "100"},
            {"symbol": "ETHUSDT", "position_side": "SHORT", "position_amount": "-1", "entry_price": "100"},
        ),
        active_order_summaries=(
            {"symbol": "ETHUSDT", "client_order_id": "external-1", "order_id": "1", "side": "BUY", "position_side": "LONG", "type": "LIMIT", "time_in_force": "GTC", "price": "90", "original_quantity": "1", "executed_quantity": "0", "reduce_only": "false", "update_time": "0"},
        ),
        wallet_balance=D("1000"),
        available_balance=D("100"),
        leverage_by_symbol={"ETHUSDT": D("10"), "BTCUSDT": D("10")},
    )
    built = facts_from_preflight(preflight, symbol="ETHUSDT", store=store, observed_at=now)
    assert built.long_quantity == D("1") and built.short_quantity == D("1")
    assert built.external_order_count == 1
    assert built.managed_orders == ()


def test_live_cancel_path_refuses_order_not_in_managed_store(tmp_path):
    # An incompatible active order makes the planner request cancellation/replacement.
    active = ActiveOrder(
        order_id="foreign-order",
        client_order_id="external-client",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        quantity=D("0.1"),
        price=D("50"),
        reduce_only=False,
        bucket=PositionBucket.TACTICAL,
        action=IntentAction.INCREASE,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        time_in_force=TimeInForce.GTC,
        layer=1,
    )
    executor = FakeExecutor()
    subject = runtime(tmp_path, mode=R5StrategyMode.TESTNET, executor=executor, policy=R5PositionPolicy.REQUIRE_ADOPTION)
    with pytest.raises(PermissionError, match="non-managed order"):
        subject.run_cycle(
            cycle_id="cycle-nonmanaged-cancel",
            facts=facts(managed=(active,)),
            signals=R5StrategySignals(long_signal=D("1")),
        )
    assert executor.cancels == []


def test_dual_control_position_adoption_manifest_is_exact_and_tamper_evident(tmp_path):
    from dataclasses import replace
    from freqtrade.hedge.r5.adoption import (
        apply_position_adoption_manifest,
        build_position_adoption_manifest,
        verify_position_adoption_manifest,
    )

    store = R5StrategyStateStore(tmp_path / "adoption.sqlite")
    snapshot = facts(long=D("1"), short=D("2"), external=3)
    operator = "operator-token-123456789"
    risk = "risk-token-1234567890"
    manifest = build_position_adoption_manifest(
        facts=snapshot,
        account_fingerprint="abcdef0123456789",
        operator_token=operator,
        risk_token=risk,
        now=snapshot.observed_at,
    )
    digest = apply_position_adoption_manifest(
        store=store,
        manifest=manifest,
        facts=snapshot,
        expected_account_fingerprint="abcdef0123456789",
        operator_token=operator,
        risk_token=risk,
        now=snapshot.observed_at + timedelta(seconds=1),
    )
    assert store.core_baseline(account_id=ACCOUNT, symbol="ETHUSDT")["LONG"][0] == D("1")
    assert len(digest) == 64
    tampered = replace(manifest, long_quantity=D("9"))
    with pytest.raises(PermissionError):
        verify_position_adoption_manifest(
            tampered,
            facts=snapshot,
            expected_account_fingerprint="abcdef0123456789",
            operator_token=operator,
            risk_token=risk,
            now=snapshot.observed_at + timedelta(seconds=1),
        )


def test_adoption_manifest_expires_and_requires_both_tokens(tmp_path):
    from freqtrade.hedge.r5.adoption import build_position_adoption_manifest, verify_position_adoption_manifest

    snapshot = facts()
    manifest = build_position_adoption_manifest(
        facts=snapshot,
        account_fingerprint="abcdef0123456789",
        operator_token="operator-token-123456789",
        risk_token="risk-token-1234567890",
        ttl_seconds=30,
        now=snapshot.observed_at,
    )
    with pytest.raises(PermissionError, match="not currently valid"):
        verify_position_adoption_manifest(
            manifest,
            facts=snapshot,
            expected_account_fingerprint="abcdef0123456789",
            operator_token="operator-token-123456789",
            risk_token="risk-token-1234567890",
            now=snapshot.observed_at + timedelta(seconds=31),
        )
    with pytest.raises(PermissionError, match="operator signature"):
        verify_position_adoption_manifest(
            manifest,
            facts=snapshot,
            expected_account_fingerprint="abcdef0123456789",
            operator_token="wrong-operator-token-1234",
            risk_token="risk-token-1234567890",
            now=snapshot.observed_at + timedelta(seconds=1),
        )


def test_managed_order_mutation_changes_cycle_input_hash(tmp_path):
    now = datetime.now(UTC)
    first_order = ActiveOrder(
        order_id="managed-1",
        client_order_id="client-1",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        quantity=D("0.1"),
        price=D("90"),
        reduce_only=False,
        bucket=PositionBucket.TACTICAL,
        action=IntentAction.INCREASE,
        created_at=now,
        time_in_force=TimeInForce.GTC,
        layer=1,
    )
    mutated = ActiveOrder(
        order_id="managed-1",
        client_order_id="client-1",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        quantity=D("0.2"),
        price=D("90"),
        reduce_only=False,
        bucket=PositionBucket.TACTICAL,
        action=IntentAction.INCREASE,
        created_at=now,
        time_in_force=TimeInForce.GTC,
        layer=1,
    )
    subject = runtime(tmp_path)
    subject.run_cycle(
        cycle_id="cycle-managed-hash",
        facts=facts(managed=(first_order,)),
        signals=R5StrategySignals(),
    )
    with pytest.raises(RuntimeError, match="different inputs"):
        subject.run_cycle(
            cycle_id="cycle-managed-hash",
            facts=facts(managed=(mutated,)),
            signals=R5StrategySignals(),
        )


def test_write_budget_is_checked_before_first_exchange_write(tmp_path):
    from dataclasses import replace

    executor = FakeExecutor()
    profile = replace(default_limit_profile(R5Stage.RESTRICTED_AUTO), write_request_budget=1)
    subject = R5ProductionStrategyRuntime(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        mode=R5StrategyMode.TESTNET,
        profile=profile,
        planner_config=planner_config(),
        store=R5StrategyStateStore(tmp_path / "write-budget.sqlite"),
        executor=executor,
        position_policy=R5PositionPolicy.REQUIRE_ADOPTION,
    )
    with pytest.raises(PermissionError, match="write-request budget"):
        subject.run_cycle(
            cycle_id="cycle-write-budget",
            facts=facts(),
            signals=R5StrategySignals(long_signal=D("1"), short_signal=D("1")),
        )
    assert executor.submits == [] and executor.cancels == []


def test_successful_managed_cancel_is_account_bound_and_marked_terminal(tmp_path):
    store = R5StrategyStateStore(tmp_path / "managed-cancel.sqlite")
    store.register_managed_order(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        planner_intent_id="planner-cancel-1",
        client_order_id="client-cancel-1",
        payload={
            "action": "INCREASE",
            "bucket": "TACTICAL",
            "position_side": "LONG",
            "order_side": "BUY",
            "order_type": "LIMIT",
            "time_in_force": "GTX",
            "layer": 1,
        },
        status="ACKNOWLEDGED",
    )
    store.claim_cycle(
        cycle_id="cancel-cycle",
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        mode="TESTNET",
        input_payload={"x": 1},
    )
    store.persist_plan(cycle_id="cancel-cycle", plan={"cancel": ["planner-cancel-1"]})
    store.begin_execution("cancel-cycle")
    executor = FakeExecutor()
    subject = R5ProductionStrategyRuntime(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        mode=R5StrategyMode.TESTNET,
        profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
        planner_config=planner_config(),
        store=store,
        executor=executor,
        position_policy=R5PositionPolicy.REQUIRE_ADOPTION,
    )
    plan = SimpleNamespace(
        risk_cancel_order_ids=("planner-cancel-1",),
        modify_order_ids=(),
        delete_order_ids=(),
        submit_orders=(),
    )
    actions, writes = subject._execute_plan("cancel-cycle", plan, facts())
    assert writes == 1
    assert actions[0]["status"] == "CANCELED"
    assert store.managed_order(
        "client-cancel-1", account_id=ACCOUNT, symbol="ETHUSDT"
    )["status"] == "CANCELED"
    assert store.managed_order(
        "client-cancel-1", account_id="binance-usdm:other", symbol="ETHUSDT"
    ) is None


def test_mainnet_limited_requires_daily_budget_and_stage_permit(tmp_path):
    with pytest.raises(ValueError, match="daily budget, stage permit, and promotion journal"):
        R5ProductionStrategyRuntime(
            account_id=ACCOUNT,
            symbol="ETHUSDT",
            mode=R5StrategyMode.MAINNET_LIMITED,
            profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
            planner_config=planner_config(),
            store=R5StrategyStateStore(tmp_path / "mainnet-limited.sqlite"),
            executor=FakeExecutor(),
            position_policy=R5PositionPolicy.REQUIRE_ADOPTION,
        )


def test_shadow_external_order_count_is_not_double_counted_across_symbols(tmp_path):
    store = R5StrategyStateStore(tmp_path / "shadow-count.sqlite")
    now = datetime.now(UTC)
    preflight = R5LivePreflight(
        readonly_status="FULL_PASS",
        hedge_mode_enabled=True,
        single_asset_mode=True,
        clock_offset_ms=0,
        futures_trading_permission=False,
        ip_restricted=False,
        withdrawals_disabled=True,
        cross_margin_symbols=("BTCUSDT", "ETHUSDT"),
        account=R5AccountSnapshot(ACCOUNT, D("1000"), D("0"), D("0"), 1, (), now),
        rules={
            "ETHUSDT": R5SymbolRules("ETHUSDT", D("0.1"), D("0.01"), D("0.01"), D("1000"), D("5")),
            "BTCUSDT": R5SymbolRules("BTCUSDT", D("0.1"), D("0.001"), D("0.001"), D("100"), D("5")),
        },
        books={
            "ETHUSDT": R5Book("ETHUSDT", D("99"), D("101"), D("100")),
            "BTCUSDT": R5Book("BTCUSDT", D("999"), D("1001"), D("1000")),
        },
        request_count=12,
        active_order_summaries=(
            {"symbol": "BTCUSDT", "client_order_id": "btc-external", "order_id": "2"},
        ),
        wallet_balance=D("1000"),
        available_balance=D("1000"),
        leverage_by_symbol={"ETHUSDT": D("10"), "BTCUSDT": D("10")},
    )
    built = facts_from_preflight(preflight, symbol="ETHUSDT", store=store, observed_at=now)
    assert built.external_order_count == 1


def test_inventory_processor_is_idempotent_and_preserves_core(tmp_path):
    from freqtrade.hedge.r5.fill_processor import (
        R5InventoryEvent,
        R5InventoryEventType,
        R5InventoryProcessor,
    )

    store = R5StrategyStateStore(tmp_path / "inventory.sqlite")
    store.adopt_core_baseline(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        long_quantity=D("1"),
        long_average_price=D("100"),
        short_quantity=D("0"),
        short_average_price=D("0"),
        manifest_sha256="b" * 64,
    )
    processor = R5InventoryProcessor(store)
    opened, replay = processor.apply(
        R5InventoryEvent(
            event_id="trade-open-1",
            account_id=ACCOUNT,
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            event_type=R5InventoryEventType.OPEN_FILL,
            quantity=D("0.5"),
            price=D("90"),
            fee=D("0.1"),
            bucket=PositionBucket.TACTICAL,
            tactical_lot_id="lot-open-1",
            layer=1,
            occurred_at=datetime.now(UTC),
        )
    )
    assert not replay
    assert opened.core_quantity == D("1") and opened.tactical_quantity == D("0.5")
    repeated, replay = processor.apply(
        R5InventoryEvent(
            event_id="trade-open-1",
            account_id=ACCOUNT,
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            event_type=R5InventoryEventType.OPEN_FILL,
            quantity=D("0.5"),
            price=D("90"),
            fee=D("0.1"),
            bucket=PositionBucket.TACTICAL,
            tactical_lot_id="lot-open-1",
            layer=1,
            occurred_at=opened.tactical_lots[0].opened_at,
        )
    )
    assert replay and repeated == opened
    reduced, replay = processor.apply(
        R5InventoryEvent(
            event_id="trade-reduce-1",
            account_id=ACCOUNT,
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            event_type=R5InventoryEventType.REDUCE_FILL,
            quantity=D("0.5"),
            price=D("110"),
            fee=D("0.1"),
            bucket=PositionBucket.TACTICAL,
            tactical_lot_id="lot-open-1",
            occurred_at=datetime.now(UTC),
        )
    )
    assert not replay
    assert reduced.core_quantity == D("1") and reduced.tactical_quantity == D("0")
    assert reduced.tactical_realized_pnl == D("10")
    assert store.integrity()["status"] == "PASS"


def test_inventory_event_id_reuse_with_different_payload_is_rejected(tmp_path):
    from dataclasses import replace
    from freqtrade.hedge.r5.fill_processor import (
        R5InventoryEvent,
        R5InventoryEventType,
        R5InventoryProcessor,
    )

    store = R5StrategyStateStore(tmp_path / "inventory-reuse.sqlite")
    processor = R5InventoryProcessor(store)
    event = R5InventoryEvent(
        event_id="same-trade-id",
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        position_side=PositionSide.SHORT,
        event_type=R5InventoryEventType.OPEN_FILL,
        quantity=D("1"),
        price=D("100"),
        bucket=PositionBucket.TACTICAL,
        tactical_lot_id="short-lot-1",
        occurred_at=datetime.now(UTC),
    )
    processor.apply(event)
    with pytest.raises(RuntimeError, match="different payload"):
        processor.apply(replace(event, price=D("101")))


def test_live_runtime_fails_closed_on_durable_inventory_drift(tmp_path):
    store = R5StrategyStateStore(tmp_path / "inventory-drift.sqlite")
    store.adopt_core_baseline(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        long_quantity=D("1"),
        long_average_price=D("100"),
        short_quantity=D("0"),
        short_average_price=D("0"),
        manifest_sha256="c" * 64,
    )
    subject = R5ProductionStrategyRuntime(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        mode=R5StrategyMode.TESTNET,
        profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
        planner_config=planner_config(),
        store=store,
        executor=FakeExecutor(),
        position_policy=R5PositionPolicy.REQUIRE_ADOPTION,
    )
    with pytest.raises(RuntimeError, match="durable inventory"):
        subject.run_cycle(
            cycle_id="inventory-drift-cycle",
            facts=facts(long=D("1.5")),
            signals=R5StrategySignals(),
        )


def test_bounded_strategy_session_runs_multiple_shadow_cycles_and_publishes_report(tmp_path):
    from freqtrade.hedge.r5.strategy_session import R5StrategySession, R5StrategySessionConfig

    subject = runtime(tmp_path)
    output = tmp_path / "session-report.json"
    session = R5StrategySession(
        runtime=subject,
        facts_provider=lambda index: facts(now=datetime.now(UTC)),
        signals_provider=lambda index, snapshot: R5StrategySignals(
            long_signal=D("1") if index % 2 == 0 else D("0"),
            short_signal=D("1") if index % 2 == 1 else D("0"),
        ),
        config=R5StrategySessionConfig(
            session_id="offline-shadow-session",
            max_cycles=3,
            max_runtime_seconds=10,
            cycle_interval_seconds=0,
        ),
        output=output,
    )
    report = session.run()
    assert report["status"] == "COMPLETED"
    assert report["cycles_completed"] == 3
    assert report["writes_attempted"] == 0
    assert report["mainnet_order_requests_sent"] == 0
    assert output.is_file()


def test_strategy_session_kill_switch_stops_before_snapshot_or_write(tmp_path):
    from freqtrade.hedge.r5.strategy_session import R5StrategySession, R5StrategySessionConfig

    calls = []
    session = R5StrategySession(
        runtime=runtime(tmp_path),
        facts_provider=lambda index: calls.append(index) or facts(),
        signals_provider=lambda index, snapshot: R5StrategySignals(),
        config=R5StrategySessionConfig(
            session_id="killed-session",
            max_cycles=5,
            max_runtime_seconds=10,
            cycle_interval_seconds=0,
        ),
        output=tmp_path / "killed.json",
        kill_switch=lambda: "OPERATOR_KILL",
    )
    report = session.run()
    assert report["status"] == "KILLED"
    assert report["stop_reason"] == "OPERATOR_KILL"
    assert report["cycles_completed"] == 0
    assert calls == []



def test_r52_installer_forces_single_report_discovery_to_array():
    project_root = Path(__file__).resolve().parents[3]
    installer = project_root / "scripts" / "Install-Freqtrade-Hedge-R5.2-ProductionStrategyCore.ps1"
    text = installer.read_text(encoding="ascii")
    assert "$R516Reports = @(" in text
    assert "if (@($R516Reports).Count -eq 0)" in text
    assert "$R516Reports.Count" not in text
    assert "foreach ($CandidateReport in @($R516Reports))" in text


def test_r52_installer_selects_a_valid_passing_prerequisite_not_just_latest_file():
    project_root = Path(__file__).resolve().parents[3]
    installer = project_root / "scripts" / "Install-Freqtrade-Hedge-R5.2-ProductionStrategyCore.ps1"
    text = installer.read_text(encoding="ascii")
    required_contract_tokens = (
        'R5_1_MAINNET_READONLY_COMPLETE_WRITES_LOCKED',
        'mainnet_readonly_complete',
        'real_mainnet_order_requests_sent',
        'mainnet_live_exchange_write',
        'required_failures',
        'No valid completed R5.1.6 mainnet-readonly acceptance report was found',
    )
    assert all(token in text for token in required_contract_tokens)
    assert '$R516Reports[0]' not in text



def test_r52_diagnostics_discovers_all_versioned_toolkit_import_roots(tmp_path):
    from tools.hedge_r52.r52_toolkit.diagnostics import _toolkit_pythonpath_entries

    root = tmp_path / "project"
    (root / "tools" / "hedge_r30" / "r30_toolkit").mkdir(parents=True)
    (root / "tools" / "hedge_r33" / "r33_toolkit").mkdir(parents=True)
    (root / "tools" / "hedge_r38" / "r38_toolkit").mkdir(parents=True)
    (root / "tools" / "hedge_misc" / "not_a_toolkit").mkdir(parents=True)
    entries = _toolkit_pythonpath_entries(root)
    assert entries[:2] == (str(root), str(root / "tools"))
    assert str(root / "tools" / "hedge_r30") in entries
    assert str(root / "tools" / "hedge_r33") in entries
    assert str(root / "tools" / "hedge_r38") in entries
    assert str(root / "tools" / "hedge_misc") not in entries


def test_r52_diagnostics_pythonpath_supports_legacy_top_level_toolkit_imports(tmp_path):
    import os
    import subprocess
    import sys

    from tools.hedge_r52.r52_toolkit.diagnostics import _toolkit_pythonpath_entries

    root = tmp_path / "project"
    for version in ("30", "33", "38"):
        package = root / "tools" / f"hedge_r{version}" / f"r{version}_toolkit"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"VALUE = {version}\n", encoding="ascii")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(_toolkit_pythonpath_entries(root))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import r30_toolkit, r33_toolkit, r38_toolkit; "
            "assert (r30_toolkit.VALUE, r33_toolkit.VALUE, r38_toolkit.VALUE) == (30, 33, 38)",
        ],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_r52_execution_compatibility_diagnostics_records_toolkit_roots():
    project_root = Path(__file__).resolve().parents[3]
    diagnostics = project_root / "tools" / "hedge_r52" / "r52_toolkit" / "diagnostics.py"
    text = diagnostics.read_text(encoding="utf-8")
    assert "_toolkit_pythonpath_entries(root)" in text
    assert '"pythonpath_toolkit_roots": list(toolkit_roots)' in text
    assert 'str(root / "tools"), current_pythonpath' not in text
