from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from freqtrade.hedge.control.auth import ConfirmationService, HedgePrincipal, HedgeRole
from freqtrade.hedge.control.models import ControlAction, ControlRequest
from freqtrade.hedge.control.service import HedgeControlService
from freqtrade.hedge.control.store import InMemoryControlOperationStore
from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.integration.paper_control import PaperAccountViewProvider
from freqtrade.hedge.integration.paper_risk_gate import apply_new_risk_gate
from freqtrade.hedge.integration.production_assembly import build_production_main_loop_assembly
from freqtrade.hedge.integration.production_main_loop import HedgeExecutionMode
from freqtrade.hedge.planning.context import (
    IntentAction,
    LegPosition,
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    PlanningResult,
    PositionBucket,
    PositionSide,
    StrategyLegState,
    WalletSnapshot,
)
from freqtrade.hedge.execution.service import PositionSide as ExecutionPositionSide

ACCOUNT = "binance-usdm:r34-paper"


class DummyPaperApplication:
    def __init__(self) -> None:
        self.account_id = ACCOUNT
        self.symbol = "ETH/USDT:USDT"
        self.execution = build_integrated_fake_runtime()
        self.last_market = MarketSnapshot(
            symbol=self.symbol,
            timestamp=datetime(2026, 8, 3, tzinfo=UTC),
            bid=Decimal("2999"),
            ask=Decimal("3001"),
            mark=Decimal("3000"),
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
        )
        self._provider = lambda: True
        self._wallet = WalletSnapshot(
            balance=Decimal("1000"),
            equity=Decimal("1010"),
            available_balance=Decimal("500"),
            long=LegPosition(
                side=PositionSide.LONG,
                quantity=Decimal("1"),
                average_price=Decimal("2900"),
                core_quantity=Decimal("1"),
                core_average_price=Decimal("2900"),
            ),
            short=LegPosition(
                side=PositionSide.SHORT,
                quantity=Decimal("2"),
                average_price=Decimal("3100"),
                tactical_quantity=Decimal("2"),
                tactical_average_price=Decimal("3100"),
            ),
            leverage=Decimal("3"),
        )

    def bind_new_risk_provider(self, provider):
        self._provider = provider

    def wallet(self, market=None):
        return self._wallet

    @property
    def new_risk_enabled(self):
        return self._provider()


def _config(mode: str) -> dict[str, object]:
    return {
        "managed_pair": "ETH/USDT:USDT",
        "hedge": {
            "account_id": ACCOUNT,
            "planner": {"cooldown_seconds": 0},
            "main_loop": {
                "enabled": True,
                "mode": mode,
                "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
                "state_backend": "memory",
            },
        },
    }


def _intent(*, side: PositionSide, reduce_only: bool) -> OrderIntent:
    if side is PositionSide.LONG:
        order_side = OrderSide.SELL if reduce_only else OrderSide.BUY
    else:
        order_side = OrderSide.BUY if reduce_only else OrderSide.SELL
    return OrderIntent.deterministic(
        symbol="ETH/USDT:USDT",
        position_side=side,
        order_side=order_side,
        action=IntentAction.CLOSE if reduce_only else IntentAction.OPEN,
        bucket=PositionBucket.TACTICAL,
        quantity=Decimal("0.1"),
        price=Decimal("3000"),
        reduce_only=reduce_only,
        reason="r34-test",
    )


def test_simulated_assembly_reuses_durable_paper_runtime_and_owns_cycle_once():
    paper = DummyPaperApplication()
    assembly = build_production_main_loop_assembly(
        config=_config("HEDGE_SIMULATED"),
        session_factory=object(),
        paper_application=paper,
    )
    assert assembly is not None
    assert assembly.execution_runtime is paper.execution
    assert assembly.exchange_write_surface == "SIMULATED"
    assert assembly.cycle_owner == "PAPER_APPLICATION"
    assembly.loop.stop_new_orders(reason="maintenance", actor="pytest")
    assert paper.new_risk_enabled is False


def test_production_locked_assembly_keeps_separate_zero_write_runtime():
    paper = DummyPaperApplication()
    assembly = build_production_main_loop_assembly(
        config=_config("HEDGE_PRODUCTION_LOCKED"),
        session_factory=object(),
        paper_application=paper,
    )
    assert assembly is not None
    assert assembly.execution_runtime is not paper.execution
    assert assembly.exchange_write_surface == "NONE"
    assert assembly.cycle_owner == "PRODUCTION_CONTROLLER"


def test_paper_account_view_exposes_both_legs_for_control_plane():
    paper = DummyPaperApplication()
    view = PaperAccountViewProvider(paper)()
    assert view.account_id == ACCOUNT
    assert {item.position_side for item in view.positions} == {"LONG", "SHORT"}
    assert {item.symbol for item in view.positions} == {"ETHUSDT"}
    assert all(item.mark_price == Decimal("3000") for item in view.positions)


def test_new_risk_gate_preserves_reduce_only_and_committed_state():
    old_long = StrategyLegState(PositionSide.LONG, sequence=3)
    old_short = StrategyLegState(PositionSide.SHORT, sequence=4)
    next_long = StrategyLegState(PositionSide.LONG, sequence=8)
    next_short = StrategyLegState(PositionSide.SHORT, sequence=9)
    increase = _intent(side=PositionSide.LONG, reduce_only=False)
    reduce = _intent(side=PositionSide.SHORT, reduce_only=True)
    planning = PlanningResult(
        ideal_orders=(increase, reduce),
        submit_orders=(increase, reduce),
        cancel_order_ids=(),
        kept_order_ids=(),
        long_state=next_long,
        short_state=next_short,
    )
    filtered, blocked = apply_new_risk_gate(
        planning,
        enabled=False,
        current_long_state=old_long,
        current_short_state=old_short,
    )
    assert blocked == 1
    assert filtered.submit_orders == (reduce,)
    assert filtered.long_state == old_long
    assert filtered.short_state == next_short
    assert "NEW_RISK_STOPPED:1" in filtered.diagnostics


def test_control_close_uses_the_same_simulated_execution_runtime():
    paper = DummyPaperApplication()
    paper.execution.account.seed(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        position_side=ExecutionPositionSide.LONG,
        quantity=Decimal("1"),
        average_price=Decimal("2900"),
    )
    assembly = build_production_main_loop_assembly(
        config=_config("HEDGE_SIMULATED"),
        session_factory=object(),
        paper_application=paper,
    )
    assert assembly is not None
    service = HedgeControlService(
        loop=assembly.loop,
        operation_store=InMemoryControlOperationStore(),
        confirmation_service=ConfirmationService(b"r" * 32),
        account_view_provider=PaperAccountViewProvider(paper),
        exchange_write_surface=assembly.exchange_write_surface,
    )
    request = ControlRequest(
        action=ControlAction.CLOSE_LONG,
        account_id=ACCOUNT,
        idempotency_key="r34-close-long",
        reason="r34 simulated close",
        symbol="ETHUSDT",
    )
    principal = HedgePrincipal("pytest", HedgeRole.RISK_MANAGER)
    token = service.issue_confirmation(principal=principal, request=request)
    result = service.execute(
        principal=principal,
        request=request,
        confirmation_token=token,
    )
    assert result.writes_attempted == 1
    order = paper.execution.core.get_order(result.executed_references[0])
    assert order.intent.reduce_only is True
    assert order.intent.position_side is ExecutionPositionSide.LONG
