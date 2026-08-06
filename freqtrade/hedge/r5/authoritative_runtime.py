"""Authoritative R5 execution and fill-writeback composition.

This module is the production composition root for strategy PlanningResult execution.
It combines SQL order/idempotency authority, direction-three atomic batch risk,
single-writer fencing, position locks, lifecycle recovery, transaction ledger/outbox,
managed-order ownership and core/tactical fill projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Sequence

from freqtrade.hedge.concurrency.position_lock import PositionLockManager
from freqtrade.hedge.concurrency.single_writer import SingleWriterGuard
from freqtrade.hedge.contracts.ports import (
    PositionKey,
    ReadinessDecision,
    ReadinessState as ContractReadinessState,
    StaticMarketRules,
)
from freqtrade.hedge.execution.action_group_store import ActionGroupRepository
from freqtrade.hedge.execution.event_publisher import InMemoryEventPublisher
from freqtrade.hedge.execution.idempotency import IdempotencyPort
from freqtrade.hedge.execution.kill_switch import KillSwitch
from freqtrade.hedge.execution.orchestrator import HedgeExecutionEngine
from freqtrade.hedge.execution.ownership import ExecutionOrderOwnershipRegistry
from freqtrade.hedge.execution.service import (
    ApprovedOrderIntent,
    ExecutionResult,
    ExecutionService,
    ExecutionStorePort,
    ExchangeExecutionPort,
    OrderIntent,
)
from freqtrade.hedge.execution.state_machine import OrderState
from freqtrade.hedge.execution.client_order_id import build_client_order_id
from freqtrade.hedge.execution.unknown_resolver import (
    InMemoryUserStreamOrderCache,
    UnknownOrderResolver,
)
from freqtrade.hedge.execution.sql_audit import SqlExecutionAuditLog
from freqtrade.hedge.planning.context import PlanningResult
from freqtrade.hedge.execution.planner_adapter import adapt_planner_intent
from freqtrade.hedge.risk.commit import RiskApprovalCommitPort
from freqtrade.hedge.risk.portfolio import RiskPortfolioSnapshot
from freqtrade.hedge.risk.runtime import HedgeRiskRuntime
from freqtrade.persistence.hedge_execution_adapters import (
    SqlActionGroupRepository,
    SqlExecutionIdempotencyStore,
    SqlExecutionLedger,
    SqlExecutionStore,
)

from .arming import R5StagePermit
from .batch_risk import R5PreparedBatchRiskApproval, R5PreparedRiskBatch
from .budget import R5BudgetPort
from .fencing import R5FencedExchangeExecutionPort
from .fill_closed_loop import R5FillProjectionReport, R5SqlFillProjector
from .emergency import R5EmergencyExitPermit
from .models import R5AccountSnapshot, R5LimitProfile


class _RiskRuntimeReadinessGate:
    """Evaluate the mutable risk/readiness monitor at every execution boundary."""

    def __init__(self, runtime: HedgeRiskRuntime) -> None:
        self.runtime = runtime

    def evaluate(self, position_key: PositionKey) -> ReadinessDecision:
        del position_key
        report = self.runtime.refresh().readiness
        if report.ready:
            return ReadinessDecision(ContractReadinessState.READY)
        state = (
            ContractReadinessState.HALT
            if report.state.value == "HALT"
            else ContractReadinessState.DEGRADED
        )
        return ReadinessDecision(
            state,
            tuple(code.value for code in report.reason_codes),
            allow_reduce=report.emergency_reduce_only_allowed,
        )


class _WriterAdapter:
    def __init__(self, writer: SingleWriterGuard) -> None:
        self.writer = writer

    def assert_leader(self, *, account_id: str, now: object) -> None:
        del account_id, now
        self.writer.assert_valid()

    def claim(self, *, account_id: str, owner_id: str) -> bool:
        del account_id, owner_id
        return self.writer.status().valid


class _PositionLockAdapter:
    def __init__(self, locks: PositionLockManager) -> None:
        self.locks = locks

    def acquire(self, position_key: PositionKey):
        return self.locks.lock(
            account_id=position_key.account_id,
            symbol=position_key.canonical_symbol,
            position_side=position_key.position_side.value,
            exchange=position_key.exchange,
        )


@dataclass(frozen=True, slots=True)
class R5AuthoritativeBatchReport:
    cancellations: tuple[ExecutionResult, ...]
    submissions: tuple[ExecutionResult, ...]
    risk_batch: R5PreparedRiskBatch | None
    fill_projection: R5FillProjectionReport
    fencing_token: int
    writes_attempted: int


@dataclass(frozen=True, slots=True)
class R5OrderTestReport:
    client_order_id: str
    accepted: bool
    fencing_token: int
    writes_attempted: int


@dataclass(frozen=True, slots=True)
class R5RecoveryReport:
    inspected: int
    recovered: tuple[ExecutionResult, ...]
    unresolved: tuple[str, ...]
    fill_projection: R5FillProjectionReport


@dataclass(frozen=True, slots=True)
class R5AuthoritativeRuntime:
    engine: HedgeExecutionEngine
    core: ExecutionService
    store: ExecutionStorePort
    idempotency: IdempotencyPort[ExecutionResult]
    exchange: R5FencedExchangeExecutionPort
    ownership: ExecutionOrderOwnershipRegistry
    writer: SingleWriterGuard
    risk: R5PreparedBatchRiskApproval
    transaction: SqlExecutionLedger
    action_groups: ActionGroupRepository
    kill_switch: KillSwitch
    fill_projector: R5SqlFillProjector
    budget: R5BudgetPort
    account_id: str

    def seed_position_projection(
        self,
        *,
        symbol: str,
        position_side: str,
        quantity: Decimal,
        average_entry_price: Decimal,
        observed_at: object | None = None,
    ) -> object:
        key = PositionKey(
            exchange="binance",
            account_id=self.account_id,
            symbol=symbol,
            position_side=position_side,
        )
        seed = getattr(self.transaction, "seed_position", None)
        if not callable(seed):
            raise RuntimeError("authoritative ledger does not support position seeding")
        return seed(
            position_key=key,
            quantity=quantity,
            average_entry_price=average_entry_price,
            observed_at=observed_at,
        )

    def execute_batch(
        self,
        *,
        portfolio: RiskPortfolioSnapshot,
        intents: Iterable[OrderIntent],
        cancel_references: Iterable[str] = (),
        permit: R5StagePermit,
        profile: R5LimitProfile,
        budget: R5BudgetPort | None = None,
        budget_reservations: Sequence[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ] = (),
        durable_namespace: str = "r5-authoritative",
    ) -> R5AuthoritativeBatchReport:
        submit_intents = tuple(intents)
        references = tuple(dict.fromkeys(str(item).strip() for item in cancel_references if str(item).strip()))
        managed_cancel_ids: list[str] = []
        for reference in references:
            order = self.ownership.resolve_managed_reference(reference)
            if order is None:
                raise PermissionError(f"R5 refuses to cancel non-managed order: {reference}")
            if order.intent.account_id != self.account_id:
                raise PermissionError("R5 managed cancellation account mismatch")
            if not order.lifecycle.terminal:
                managed_cancel_ids.append(order.client_order_id)

        required_writes = len(managed_cancel_ids) + len(submit_intents)
        permit.preflight(required_writes)
        lease = self.writer.assert_valid()
        if budget is not None and budget is not self.budget:
            raise PermissionError("R5 authoritative runtime rejects an alternate budget store")
        effective_budget = self.budget
        reservation_rows = tuple(budget_reservations)
        if submit_intents and len(reservation_rows) != len(submit_intents):
            raise PermissionError("R5 budget reservation coverage is incomplete")
        budget_reservation = None
        if reservation_rows:
            budget_reservation = effective_budget.prepare_orders(
                account_id=self.account_id,
                profile=profile,
                reservations=reservation_rows,
            )

        risk_batch: R5PreparedRiskBatch | None = None
        if submit_intents:
            risk_batch = self.risk.prepare(
                portfolio=portfolio,
                intents=submit_intents,
                expected_fencing_token=lease.fencing_token,
                durable_namespace=durable_namespace,
            )
        self.exchange.bind_fencing_token(lease.fencing_token)
        cancellations: list[ExecutionResult] = []
        submissions: list[ExecutionResult] = []
        writes = 0
        try:
            # Risk-reducing cancellations execute before submissions and ownership is SQL-based.
            for client_id in managed_cancel_ids:
                permit.consume("AUTHORITATIVE_CANCEL")
                result = self.engine.cancel(client_id)
                writes += 1
                if result.order.lifecycle.status is OrderState.UNKNOWN:
                    result = self.engine.resolve_unknown(client_id)
                cancellations.append(result)
            for intent in submit_intents:
                permit.consume("AUTHORITATIVE_SUBMIT")
                result = self.engine.submit(intent)
                writes += 1
                if result.order.lifecycle.status is OrderState.UNKNOWN:
                    result = self.engine.resolve_unknown(result.order.client_order_id)
                submissions.append(result)
        except Exception:
            if budget_reservation is not None:
                if writes == 0:
                    effective_budget.release_reservation(budget_reservation.reservation_id)
                else:
                    effective_budget.confirm_reservation(budget_reservation.reservation_id)
            raise
        finally:
            self.exchange.clear_fencing_token()
            self.risk.clear()
        if budget_reservation is not None:
            effective_budget.confirm_reservation(budget_reservation.reservation_id)
        projection = self.fill_projector.project(account_id=self.account_id)
        return R5AuthoritativeBatchReport(
            tuple(cancellations),
            tuple(submissions),
            risk_batch,
            projection,
            lease.fencing_token,
            writes,
        )

    def execute_planning_result(
        self,
        *,
        result: PlanningResult,
        portfolio: RiskPortfolioSnapshot,
        permit: R5StagePermit,
        profile: R5LimitProfile,
        cycle_id: str,
        strategy_id: str = "R5.3_AUTHORITATIVE",
        budget: R5BudgetPort | None = None,
        budget_reservations: Sequence[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ] = (),
    ) -> R5AuthoritativeBatchReport:
        cancel_references = tuple(
            dict.fromkeys(
                (
                    *result.risk_cancel_order_ids,
                    *result.cancel_order_ids,
                    *result.modify_order_ids,
                    *result.delete_order_ids,
                )
            )
        )
        intents = tuple(
            adapt_planner_intent(
                item,
                account_id=self.account_id,
                strategy_id=strategy_id,
                cycle_id=cycle_id,
            )
            for item in sorted(
                result.submit_orders,
                key=lambda value: (
                    not value.reduce_only,
                    value.position_side.value,
                    value.layer,
                    value.intent_id,
                ),
            )
        )
        return self.execute_batch(
            portfolio=portfolio,
            intents=intents,
            cancel_references=cancel_references,
            permit=permit,
            profile=profile,
            budget=budget,
            budget_reservations=budget_reservations,
            durable_namespace=f"r5-planning:{cycle_id}",
        )

    def execute_emergency_reduce(
        self,
        *,
        portfolio: RiskPortfolioSnapshot,
        intent: OrderIntent,
        emergency_permit: R5EmergencyExitPermit,
        stage_permit: R5StagePermit,
        profile: R5LimitProfile,
        budget: R5BudgetPort | None = None,
        budget_reservations: Sequence[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ] = (),
    ) -> R5AuthoritativeBatchReport:
        """Execute one dual-authorized, reduce-only emergency MARKET intent.

        Normal stage authorization, SQL budget, official cumulative risk, position
        locking, single-writer fencing and transaction ledger remain mandatory.
        """
        emergency_permit.authorize(intent)
        return self.execute_batch(
            portfolio=portfolio,
            intents=(intent,),
            permit=stage_permit,
            profile=profile,
            budget=budget,
            budget_reservations=budget_reservations,
            durable_namespace=f"r5-emergency:{emergency_permit.permit_id}",
        )

    def validate_order_test(
        self,
        *,
        intent: OrderIntent,
        permit: R5StagePermit,
        profile: R5LimitProfile,
    ) -> R5OrderTestReport:
        """Validate one order through /order/test without creating order state.

        This endpoint has no matching-engine side effect, but it still consumes the
        stage write budget, validates exact account/symbol/notional constraints and
        rechecks the single-writer fencing token at the HTTP boundary.
        """
        if profile.stage.value != "ORDER_TEST":
            raise PermissionError("R5 order-test validation requires ORDER_TEST stage")
        if intent.account_id != self.account_id:
            raise PermissionError("R5 order-test account mismatch")
        reference = intent.limit_price
        if reference is None:
            reference = Decimal(str(intent.metadata.get("reference_price", "0")))
        if reference <= 0 or intent.quantity * reference > profile.max_single_order_notional:
            raise PermissionError("R5 order-test notional limit exceeded")
        permit.preflight(1)
        lease = self.writer.assert_valid()
        client_id = build_client_order_id(
            account_id=intent.account_id,
            symbol=intent.symbol,
            position_side=intent.position_side.value,
            idempotency_key=intent.idempotency_key,
            prefix="FTH",
        )
        approved = ApprovedOrderIntent(
            intent=intent,
            approved_quantity=intent.quantity,
            client_order_id=client_id,
            approved_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            risk_reason_codes=("ORDER_TEST_NO_MATCHING_ENGINE_EFFECT",),
        )
        self.exchange.bind_fencing_token(lease.fencing_token)
        try:
            permit.consume("AUTHORITATIVE_ORDER_TEST")
            result = self.exchange.validate_order(approved)
        finally:
            self.exchange.clear_fencing_token()
        return R5OrderTestReport(
            client_order_id=client_id,
            accepted=bool(getattr(result, "accepted", False)),
            fencing_token=lease.fencing_token,
            writes_attempted=1,
        )

    def recover(self, *, symbols: tuple[str, ...] = ()) -> R5RecoveryReport:
        recovered: list[ExecutionResult] = []
        unresolved: list[str] = []
        orders = tuple(self.store.list_orders())
        for order in orders:
            if order.intent.account_id != self.account_id:
                continue
            if symbols and order.intent.symbol not in symbols:
                continue
            status = order.lifecycle.status
            if status in {OrderState.PREPARED, OrderState.SUBMITTING}:
                # ExecutionService detects the existing deterministic record and queries
                # before any possible resubmission.
                result = self.core.submit(order.intent)
            elif status is OrderState.UNKNOWN:
                result = self.engine.resolve_unknown(order.client_order_id)
            elif status in {OrderState.ACKNOWLEDGED, OrderState.PARTIAL}:
                result = self.engine.refresh_order(order.client_order_id)
            else:
                continue
            recovered.append(result)
            if result.order.lifecycle.status is OrderState.UNKNOWN:
                unresolved.append(result.order.client_order_id)
        projection = self.fill_projector.project(
            account_id=self.account_id,
            symbols=symbols,
        )
        return R5RecoveryReport(
            inspected=len(orders),
            recovered=tuple(recovered),
            unresolved=tuple(unresolved),
            fill_projection=projection,
        )


def build_r5_authoritative_runtime(
    *,
    account_id: str,
    session_factory: object,
    writer: SingleWriterGuard,
    position_locks: PositionLockManager,
    risk_runtime: HedgeRiskRuntime,
    risk_commit_store: RiskApprovalCommitPort,
    exchange: ExchangeExecutionPort | None = None,
    exchange_factory: Callable[[ExecutionStorePort], ExchangeExecutionPort] | None = None,
    fill_projector: R5SqlFillProjector,
    budget: R5BudgetPort,
    leverage: Decimal,
    maintenance_margin_rate: Decimal,
) -> R5AuthoritativeRuntime:
    """Build the one authoritative execution graph for Fake/Testnet/Mainnet.

    The supplied exchange decides environment; all risk, SQL authority, lifecycle,
    ownership, fencing and fill projection semantics are identical.
    """
    if (exchange is None) == (exchange_factory is None):
        raise ValueError("exactly one of exchange or exchange_factory is required")
    store = SqlExecutionStore(session_factory)
    delegate_exchange = exchange if exchange is not None else exchange_factory(store)
    idempotency = SqlExecutionIdempotencyStore(session_factory, store)
    transaction = SqlExecutionLedger(session_factory)
    audit = SqlExecutionAuditLog(session_factory, account_id=account_id)
    prepared_risk = R5PreparedBatchRiskApproval(
        runtime=risk_runtime,
        commit_store=risk_commit_store,
        leverage=leverage,
        maintenance_margin_rate=maintenance_margin_rate,
    )
    fenced_exchange = R5FencedExchangeExecutionPort(delegate_exchange, writer=writer)
    cache = InMemoryUserStreamOrderCache()
    resolver = UnknownOrderResolver(fenced_exchange, user_stream_cache=cache)
    kill_switch = KillSwitch(allow_risk_reduction_while_halted=True)
    from freqtrade.hedge.telemetry.metrics import HedgeMetrics

    core = ExecutionService(
        risk=prepared_risk,
        exchange=fenced_exchange,
        store=store,
        idempotency=idempotency,
        unknown_resolver=resolver,
        kill_switch=kill_switch,
        audit=audit,
        metrics=HedgeMetrics(),
    )
    engine = HedgeExecutionEngine(
        core,
        readiness=_RiskRuntimeReadinessGate(risk_runtime),
        single_writer=_WriterAdapter(writer),
        position_lock=_PositionLockAdapter(position_locks),
        market_rules=StaticMarketRules(),
        transaction=transaction,
        publisher=InMemoryEventPublisher(),
        exchange="binance",
        user_stream_cache=cache,
        strict_dependencies=True,
    )
    return R5AuthoritativeRuntime(
        engine=engine,
        core=core,
        store=store,
        idempotency=idempotency,
        exchange=fenced_exchange,
        ownership=ExecutionOrderOwnershipRegistry(store),
        writer=writer,
        risk=prepared_risk,
        transaction=transaction,
        action_groups=SqlActionGroupRepository(session_factory),
        kill_switch=kill_switch,
        fill_projector=fill_projector,
        budget=budget,
        account_id=account_id,
    )
