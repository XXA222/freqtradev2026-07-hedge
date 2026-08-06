from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Protocol

from freqtrade.hedge.contracts.adapters import adapt_planner_intent
from freqtrade.hedge.execution.service import ExecutionResult, OrderIntent as ExecutionIntent, OrderType
from freqtrade.hedge.execution.state_machine import OrderState
from freqtrade.hedge.planning.context import (
    ActiveOrder,
    LegPosition,
    MarketSnapshot,
    PlannerConfig,
    PlanningContext,
    PositionSide as PlanningSide,
    StrategyLegState,
    WalletSnapshot,
    ZERO,
)
from freqtrade.hedge.r4.planner import R4HedgePlanner, R4PlanningInputs

from .arming import R5StagePermit
from .budget import R5DailyBudgetStore
from .journal import R5Journal
from .models import R5AccountSnapshot, R5LimitProfile, R5Stage
from .strategy_store import R5StrategyStateStore


class R5StrategyMode(StrEnum):
    SHADOW = "SHADOW"
    DRY_RUN = "DRY_RUN"
    TESTNET = "TESTNET"
    MAINNET_LOCKED = "MAINNET_LOCKED"
    MAINNET_LIMITED = "MAINNET_LIMITED"

    @property
    def allows_exchange_write(self) -> bool:
        return self in {R5StrategyMode.TESTNET, R5StrategyMode.MAINNET_LIMITED}


class R5PositionPolicy(StrEnum):
    PROTECT_AS_CORE = "PROTECT_AS_CORE"
    REQUIRE_ADOPTION = "REQUIRE_ADOPTION"


@dataclass(frozen=True, slots=True)
class R5StrategySignals:
    long_signal: Decimal = ZERO
    short_signal: Decimal = ZERO
    target_net_quantity: Decimal | None = None
    realized_volatility: Decimal = ZERO
    funding_rate: Decimal = ZERO

    def __post_init__(self) -> None:
        values = (self.long_signal, self.short_signal, self.realized_volatility, self.funding_rate)
        if any(not value.is_finite() for value in values):
            raise ValueError("R5.2 strategy signals must be finite")
        if self.realized_volatility < ZERO:
            raise ValueError("R5.2 realized volatility cannot be negative")
        if self.target_net_quantity is not None and not self.target_net_quantity.is_finite():
            raise ValueError("R5.2 target net quantity must be finite")


@dataclass(frozen=True, slots=True)
class R5StrategyFacts:
    account_id: str
    symbol: str
    observed_at: datetime
    market: MarketSnapshot
    balance: Decimal
    equity: Decimal
    available_balance: Decimal
    leverage: Decimal
    long_quantity: Decimal
    long_average_price: Decimal
    short_quantity: Decimal
    short_average_price: Decimal
    managed_orders: tuple[ActiveOrder, ...] = ()
    external_order_count: int = 0
    unmanaged_positions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("R5.2 facts observed_at must be timezone-aware")
        if self.market.symbol != self.symbol:
            raise ValueError("R5.2 market symbol mismatch")
        if not self.account_id.startswith("binance-usdm:"):
            raise ValueError("R5.2 account namespace is invalid")
        values = (
            self.balance,
            self.equity,
            self.available_balance,
            self.leverage,
            self.long_quantity,
            self.long_average_price,
            self.short_quantity,
            self.short_average_price,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("R5.2 fact values must be finite")
        if min(self.balance, self.equity, self.available_balance, self.long_quantity, self.short_quantity) < ZERO:
            raise ValueError("R5.2 fact values cannot be negative")
        if self.leverage <= ZERO:
            raise ValueError("R5.2 leverage must be positive")
        if self.long_quantity > ZERO and self.long_average_price <= ZERO:
            raise ValueError("R5.2 LONG average price is invalid")
        if self.short_quantity > ZERO and self.short_average_price <= ZERO:
            raise ValueError("R5.2 SHORT average price is invalid")
        if self.long_quantity == ZERO and self.long_average_price != ZERO:
            raise ValueError("R5.2 flat LONG average price must be zero")
        if self.short_quantity == ZERO and self.short_average_price != ZERO:
            raise ValueError("R5.2 flat SHORT average price must be zero")
        if isinstance(self.external_order_count, bool) or self.external_order_count < 0:
            raise ValueError("R5.2 external order count is invalid")

    @property
    def gross_notional(self) -> Decimal:
        return (self.long_quantity + self.short_quantity) * self.market.mark

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "account_id": self.account_id,
            "symbol": self.symbol,
            "observed_at": self.observed_at.isoformat(),
            "market": {
                "timestamp": self.market.timestamp.isoformat(),
                "bid": str(self.market.bid),
                "ask": str(self.market.ask),
                "mark": str(self.market.mark),
                "tick_size": str(self.market.tick_size),
                "qty_step": str(self.market.qty_step),
                "min_qty": str(self.market.min_qty),
                "min_notional": str(self.market.min_notional),
            },
            "balance": str(self.balance),
            "equity": str(self.equity),
            "available_balance": str(self.available_balance),
            "leverage": str(self.leverage),
            "long_quantity": str(self.long_quantity),
            "long_average_price": str(self.long_average_price),
            "short_quantity": str(self.short_quantity),
            "short_average_price": str(self.short_average_price),
            "managed_orders": sorted(
                (
                    {
                        "order_id": item.order_id,
                        "client_order_id": item.client_order_id,
                        "position_side": item.position_side.value,
                        "order_side": item.order_side.value,
                        "quantity": str(item.quantity),
                        "price": str(item.price),
                        "reduce_only": item.reduce_only,
                        "bucket": item.bucket.value,
                        "action": item.action.value,
                        "order_type": item.order_type.value,
                        "time_in_force": item.time_in_force.value,
                        "layer": item.layer,
                        "tactical_lot_id": item.tactical_lot_id,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in self.managed_orders
                ),
                key=lambda item: (item["client_order_id"], item["order_id"]),
            ),
            "external_order_count": self.external_order_count,
            "unmanaged_positions": sorted(self.unmanaged_positions),
        }


class R5StrategyExecutorPort(Protocol):
    def submit(self, intent: ExecutionIntent) -> ExecutionResult: ...
    def cancel(self, client_order_id: str) -> ExecutionResult: ...
    def telemetry(self) -> object: ...


@dataclass(frozen=True, slots=True)
class R5StrategyCycleResult:
    cycle_id: str
    mode: R5StrategyMode
    status: str
    plan_sha256: str
    writes_attempted: int
    cancel_count: int
    submit_count: int
    kept_count: int
    external_order_count: int
    long_target_quantity: Decimal
    short_target_quantity: Decimal
    net_gap_quantity: Decimal
    risk_flags: tuple[str, ...]
    diagnostics: tuple[str, ...]
    report: Mapping[str, Any]


class R5ProductionStrategyRuntime:
    """Production-equivalent planner-to-execution runtime for the original hedge goal.

    The same deterministic R4 planner is used in every mode.  SHADOW, DRY_RUN, and
    MAINNET_LOCKED persist the complete plan but cannot call the executor.  TESTNET and
    MAINNET_LIMITED execute only managed-order cancellations and validated LIMIT intents.
    """

    def __init__(
        self,
        *,
        account_id: str,
        symbol: str,
        mode: R5StrategyMode,
        profile: R5LimitProfile,
        planner_config: PlannerConfig,
        store: R5StrategyStateStore,
        executor: R5StrategyExecutorPort | None = None,
        planner: R4HedgePlanner | None = None,
        position_policy: R5PositionPolicy = R5PositionPolicy.PROTECT_AS_CORE,
        budget: R5DailyBudgetStore | None = None,
        permit: R5StagePermit | None = None,
        promotion_journal: R5Journal | None = None,
        max_fact_age_seconds: int = 30,
    ) -> None:
        if symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("R5.2 symbol is not allowlisted")
        if max_fact_age_seconds <= 0 or max_fact_age_seconds > 300:
            raise ValueError("R5.2 fact age limit is invalid")
        if mode.allows_exchange_write and executor is None:
            raise ValueError("R5.2 write mode requires an executor")
        if not mode.allows_exchange_write and executor is not None:
            raise ValueError("R5.2 non-write mode must not receive an executor")
        if mode is R5StrategyMode.MAINNET_LIMITED and (
            budget is None or permit is None or promotion_journal is None
        ):
            raise ValueError(
                "R5.2 MAINNET_LIMITED requires daily budget, stage permit, and promotion journal"
            )
        if mode is R5StrategyMode.MAINNET_LIMITED and profile.stage not in {
            R5Stage.RESTRICTED_AUTO,
            R5Stage.EXPANDED_LIMITS,
        }:
            raise ValueError("R5.2 MAINNET_LIMITED requires an automatic-production stage profile")
        if permit is not None and permit.stage is not profile.stage:
            raise ValueError("R5.2 stage permit and limit profile do not match")
        self.account_id = account_id
        self.symbol = symbol
        self.mode = mode
        self.profile = profile
        self.planner_config = planner_config
        self.store = store
        self.executor = executor
        self.planner = planner or R4HedgePlanner()
        self.position_policy = position_policy
        self.budget = budget
        self.permit = permit
        self.promotion_journal = promotion_journal
        self.max_fact_age_seconds = max_fact_age_seconds

    def run_cycle(
        self,
        *,
        cycle_id: str,
        facts: R5StrategyFacts,
        signals: R5StrategySignals,
    ) -> R5StrategyCycleResult:
        started = datetime.now(UTC)
        self._validate_facts(facts, started)
        input_payload = {
            "facts": facts.canonical_payload(),
            "signals": {
                "long_signal": str(signals.long_signal),
                "short_signal": str(signals.short_signal),
                "target_net_quantity": None if signals.target_net_quantity is None else str(signals.target_net_quantity),
                "realized_volatility": str(signals.realized_volatility),
                "funding_rate": str(signals.funding_rate),
            },
            "mode": self.mode.value,
            "profile": self.profile.to_dict(),
            "position_policy": self.position_policy.value,
        }
        replay_report, replay = self.store.claim_cycle(
            cycle_id=cycle_id,
            account_id=self.account_id,
            symbol=self.symbol,
            mode=self.mode.value,
            input_payload=input_payload,
        )
        if replay:
            if replay_report is None:
                row = self.store.cycle(cycle_id)
                raise RuntimeError(f"R5.2 interrupted cycle requires recovery: {row['status'] if row else 'UNKNOWN'}")
            return self._result_from_report(replay_report)

        writes = 0
        recovery_required = False
        try:
            context, context_diagnostics = self._build_context(facts, signals)
            effective_config = self._effective_config(facts.equity)
            context = replace(context, config=effective_config)
            decision = self.planner.plan(
                R4PlanningInputs(
                    context=context,
                    realized_volatility=signals.realized_volatility,
                    funding_rate=signals.funding_rate,
                    max_single_order_notional_cap=self.profile.max_single_order_notional,
                    max_gross_wallet_exposure_cap=self._profile_gross_ratio(facts.equity),
                )
            )
            plan_payload = self._plan_payload(decision.result, decision.risk_flags, context_diagnostics)
            plan_sha = self.store.persist_plan(cycle_id=cycle_id, plan=plan_payload)

            actions: list[Mapping[str, Any]] = []
            if self.mode.allows_exchange_write:
                self._assert_live_write_preconditions(facts)
                self.store.begin_execution(cycle_id)
                recovery_required = True
                actions, writes = self._execute_plan(cycle_id, decision.result, facts)
                recovery_required = False
            else:
                actions = self._shadow_actions(decision.result)

            completed = datetime.now(UTC)
            report: dict[str, Any] = {
                "schema_version": 1,
                "toolkit_version": "5.2.3",
                "cycle_id": cycle_id,
                "mode": self.mode.value,
                "status": "PASS",
                "account_id": self.account_id,
                "symbol": self.symbol,
                "started_at": started.isoformat(),
                "completed_at": completed.isoformat(),
                "writes_attempted": writes,
                "exchange_write": "ENABLED_LIMITED" if self.mode.allows_exchange_write else "LOCKED",
                "plan_sha256": plan_sha,
                "planner": plan_payload,
                "actions": actions,
                "external_order_count": facts.external_order_count,
                "unmanaged_positions": list(facts.unmanaged_positions),
                "risk_flags": list(decision.risk_flags),
                "diagnostics": list((*context_diagnostics, *decision.result.diagnostics)),
                "long_target_quantity": str(decision.result.long_target_quantity),
                "short_target_quantity": str(decision.result.short_target_quantity),
                "target_net_quantity": str(decision.result.target_net_quantity),
                "net_gap_quantity": str(decision.result.net_gap_quantity),
                "mainnet_order_requests_sent": writes if self.mode is R5StrategyMode.MAINNET_LIMITED else 0,
            }
            canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            report["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
            self.store.complete_cycle(
                cycle_id=cycle_id,
                report=report,
                long_state=decision.result.long_state,
                short_state=decision.result.short_state,
            )
            return self._result_from_report(report)
        except Exception as exc:
            self.store.fail_cycle(
                cycle_id=cycle_id,
                error=f"{type(exc).__name__}:{exc}",
                recovery_required=recovery_required or writes > 0,
            )
            raise

    def _build_context(
        self,
        facts: R5StrategyFacts,
        signals: R5StrategySignals,
    ) -> tuple[PlanningContext, tuple[str, ...]]:
        baseline = self.store.core_baseline(account_id=self.account_id, symbol=self.symbol)
        diagnostics: list[str] = []
        long_inventory = self.store.inventory_leg(
            account_id=self.account_id, symbol=self.symbol, side=PlanningSide.LONG
        )
        short_inventory = self.store.inventory_leg(
            account_id=self.account_id, symbol=self.symbol, side=PlanningSide.SHORT
        )
        long = self._leg(
            side=PlanningSide.LONG,
            actual_quantity=facts.long_quantity,
            actual_average=facts.long_average_price,
            baseline=baseline.get("LONG"),
            inventory=long_inventory,
            diagnostics=diagnostics,
        )
        short = self._leg(
            side=PlanningSide.SHORT,
            actual_quantity=facts.short_quantity,
            actual_average=facts.short_average_price,
            baseline=baseline.get("SHORT"),
            inventory=short_inventory,
            diagnostics=diagnostics,
        )
        long_state, short_state = self.store.load_leg_states(account_id=self.account_id, symbol=self.symbol)
        wallet = WalletSnapshot(
            balance=facts.balance,
            equity=facts.equity,
            available_balance=facts.available_balance,
            long=long,
            short=short,
            active_orders=facts.managed_orders,
            leverage=facts.leverage,
        )
        return (
            PlanningContext(
                market=facts.market,
                wallet=wallet,
                config=self.planner_config,
                long_state=long_state,
                short_state=short_state,
                long_signal=signals.long_signal,
                short_signal=signals.short_signal,
                target_net_quantity=signals.target_net_quantity,
            ),
            tuple(diagnostics),
        )

    def _leg(
        self,
        *,
        side: PlanningSide,
        actual_quantity: Decimal,
        actual_average: Decimal,
        baseline: tuple[Decimal, Decimal] | None,
        inventory: LegPosition | None,
        diagnostics: list[str],
    ) -> LegPosition:
        if inventory is not None:
            if inventory.quantity == actual_quantity:
                diagnostics.append(f"{side.value}:DURABLE_INVENTORY_BOUND")
                return inventory
            if self.mode.allows_exchange_write:
                raise RuntimeError(
                    f"R5.2 {side.value} durable inventory does not match live quantity"
                )
            diagnostics.append(f"{side.value}:DURABLE_INVENTORY_DRIFT_SHADOW_FALLBACK")
        if baseline is None:
            if actual_quantity > ZERO and self.position_policy is R5PositionPolicy.REQUIRE_ADOPTION:
                raise PermissionError(f"R5.2 {side.value} position requires explicit adoption")
            if actual_quantity > ZERO:
                diagnostics.append(f"{side.value}:UNADOPTED_POSITION_PROTECTED_AS_CORE")
            return LegPosition(
                side=side,
                quantity=actual_quantity,
                average_price=actual_average if actual_quantity > ZERO else ZERO,
                core_quantity=actual_quantity,
                core_average_price=actual_average if actual_quantity > ZERO else ZERO,
                tactical_quantity=ZERO,
                tactical_average_price=ZERO,
            )
        core_quantity, core_average = baseline
        if core_quantity > actual_quantity:
            raise RuntimeError(f"R5.2 {side.value} actual quantity fell below adopted core baseline")
        tactical = actual_quantity - core_quantity
        return LegPosition(
            side=side,
            quantity=actual_quantity,
            average_price=actual_average if actual_quantity > ZERO else ZERO,
            core_quantity=core_quantity,
            core_average_price=core_average if core_quantity > ZERO else ZERO,
            tactical_quantity=tactical,
            tactical_average_price=actual_average if tactical > ZERO else ZERO,
        )

    def _effective_config(self, equity: Decimal) -> PlannerConfig:
        ratio = self._profile_gross_ratio(equity)
        return replace(
            self.planner_config,
            max_single_order_notional=min(
                self.profile.max_single_order_notional,
                self.planner_config.max_single_order_notional
                if self.planner_config.max_single_order_notional > ZERO
                else self.profile.max_single_order_notional,
            ),
            max_gross_wallet_exposure=min(self.planner_config.max_gross_wallet_exposure, ratio),
            max_wallet_exposure_long=min(self.planner_config.max_wallet_exposure_long, ratio),
            max_wallet_exposure_short=min(self.planner_config.max_wallet_exposure_short, ratio),
        )

    def _profile_gross_ratio(self, equity: Decimal) -> Decimal:
        if equity <= ZERO:
            return Decimal("0.00000001")
        return min(Decimal("1"), self.profile.max_gross_notional / equity)

    def _assert_live_write_preconditions(self, facts: R5StrategyFacts) -> None:
        if self.mode is R5StrategyMode.MAINNET_LIMITED:
            if self.promotion_journal is None:  # pragma: no cover - constructor prevents this
                raise RuntimeError("R5.2 promotion journal is unavailable")
            self.promotion_journal.assert_previous_complete(
                account_id=self.account_id,
                stage=self.profile.stage,
                max_age_seconds=86400,
            )
        if facts.external_order_count:
            raise PermissionError("R5.2 live execution is blocked by external active orders")
        if facts.unmanaged_positions:
            raise PermissionError("R5.2 live execution is blocked by unmanaged positions")
        if self.position_policy is not R5PositionPolicy.REQUIRE_ADOPTION:
            raise PermissionError("R5.2 live execution requires explicit position adoption policy")

    def _execute_plan(
        self,
        cycle_id: str,
        result,
        facts: R5StrategyFacts,
    ) -> tuple[list[Mapping[str, Any]], int]:
        if self.executor is None:  # pragma: no cover - constructor prevents this
            raise RuntimeError("R5.2 executor is unavailable")

        cancel_refs = tuple(
            dict.fromkeys(
                (*result.risk_cancel_order_ids, *result.modify_order_ids, *result.delete_order_ids)
            )
        )
        cancel_records: list[Mapping[str, Any]] = []
        for reference in cancel_refs:
            record = self.store.managed_order(
                reference,
                account_id=self.account_id,
                symbol=self.symbol,
            )
            if record is None:
                raise PermissionError(f"R5.2 refuses to cancel non-managed order: {reference}")
            cancel_records.append(record)

        submit_orders = sorted(
            result.submit_orders,
            key=lambda item: (
                not item.reduce_only,
                item.position_side.value,
                item.layer,
                item.intent_id,
            ),
        )
        prepared: list[tuple[object, ExecutionIntent, Decimal]] = []
        for planner_intent in submit_orders:
            execution_intent = adapt_planner_intent(
                planner_intent,
                account_id=self.account_id,
                strategy_id="R5.2_CORE_TACTICAL",
                cycle_id=cycle_id,
            )
            if execution_intent.order_type is not OrderType.LIMIT:
                raise PermissionError("R5.2 permits LIMIT orders only")
            metadata = dict(execution_intent.metadata)
            metadata["time_in_force"] = "GTC" if execution_intent.reduce_only else "GTX"
            execution_intent = replace(execution_intent, metadata=metadata)
            notional = execution_intent.quantity * (execution_intent.limit_price or ZERO)
            if notional <= ZERO or notional > self.profile.max_single_order_notional:
                raise PermissionError("R5.2 planner intent exceeds the stage single-order limit")
            prepared.append((planner_intent, execution_intent, notional))

        required_writes = len(cancel_records) + len(prepared)
        if required_writes > self.profile.write_request_budget:
            raise PermissionError("R5.2 plan exceeded the stage write-request budget")
        if self.permit is not None and required_writes > self.permit.remaining_writes:
            raise PermissionError("R5.2 stage permit has insufficient write budget")

        reservations = self._preflight_execution_batch(
            facts=facts,
            cancel_count=len(cancel_records),
            prepared=prepared,
        )
        if self.budget is not None and reservations:
            self.budget.reserve_orders(
                account_id=self.account_id,
                profile=self.profile,
                reservations=reservations,
            )

        actions: list[Mapping[str, Any]] = []
        writes = 0
        for record in cancel_records:
            client_id = str(record["client_order_id"])
            self.store.append_event(
                cycle_id=cycle_id,
                event="CANCEL_PREPARED",
                details={
                    "planner_intent_id": str(record["planner_intent_id"]),
                    "client_order_id": client_id,
                },
            )
            if self.permit is not None:
                self.permit.consume("STRATEGY_CANCEL")
            canceled = self.executor.cancel(client_id)
            writes += 1
            state = canceled.order.lifecycle.status
            if state is OrderState.UNKNOWN:
                raise RuntimeError("R5.2 cancellation status is UNKNOWN")
            self.store.update_managed_order_status(
                client_order_id=client_id,
                status=state.value,
            )
            actions.append(
                {
                    "operation": "CANCEL",
                    "client_order_id": client_id,
                    "status": state.value,
                }
            )

        for planner_intent, execution_intent, _notional in prepared:
            self.store.append_event(
                cycle_id=cycle_id,
                event="SUBMIT_PREPARED",
                details={
                    "planner_intent_id": planner_intent.intent_id,
                    "symbol": execution_intent.symbol,
                    "position_side": execution_intent.position_side.value,
                    "reduce_only": execution_intent.reduce_only,
                    "quantity": str(execution_intent.quantity),
                    "limit_price": str(execution_intent.limit_price),
                },
            )
            if self.permit is not None:
                self.permit.consume("STRATEGY_SUBMIT")
            submitted = self.executor.submit(execution_intent)
            writes += 1
            state = submitted.order.lifecycle.status
            if state is OrderState.UNKNOWN:
                raise RuntimeError("R5.2 submission status is UNKNOWN")
            self.store.register_managed_order(
                account_id=self.account_id,
                symbol=self.symbol,
                planner_intent_id=planner_intent.intent_id,
                client_order_id=submitted.order.client_order_id,
                payload=self._planner_order(planner_intent),
                status=state.value,
            )
            actions.append(
                {
                    "operation": "SUBMIT",
                    "planner_intent_id": planner_intent.intent_id,
                    "client_order_id": submitted.order.client_order_id,
                    "status": state.value,
                    "reduce_only": execution_intent.reduce_only,
                }
            )
        return actions, writes

    def _preflight_execution_batch(
        self,
        *,
        facts: R5StrategyFacts,
        cancel_count: int,
        prepared: list[tuple[object, ExecutionIntent, Decimal]],
    ) -> tuple[tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot, bool], ...]:
        current = R5AccountSnapshot(
            account_id=self.account_id,
            equity=facts.equity,
            long_notional=facts.long_quantity * facts.market.mark,
            short_notional=facts.short_quantity * facts.market.mark,
            open_orders=max(0, len(facts.managed_orders) + facts.external_order_count - cancel_count),
            unmanaged_positions=facts.unmanaged_positions,
            observed_at=facts.observed_at,
        )
        reservations: list[tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot, bool]] = []
        for _planner_intent, intent, notional in prepared:
            previous = current
            add = not intent.reduce_only
            long_value = previous.long_notional
            short_value = previous.short_notional
            if intent.position_side.value == "LONG":
                long_value = long_value + notional if add else max(ZERO, long_value - notional)
            else:
                short_value = short_value + notional if add else max(ZERO, short_value - notional)
            tif = str(intent.metadata.get("time_in_force", "")).upper()
            next_open_orders = previous.open_orders + (1 if tif in {"GTC", "GTX"} else 0)
            projected = R5AccountSnapshot(
                account_id=self.account_id,
                equity=facts.equity,
                long_notional=long_value,
                short_notional=short_value,
                open_orders=next_open_orders,
                unmanaged_positions=facts.unmanaged_positions,
                observed_at=facts.observed_at,
            )
            if projected.open_orders > self.profile.max_open_orders:
                raise PermissionError("R5.2 projected open-order count exceeds the stage profile")
            over_exposure = (
                projected.gross_notional > self.profile.max_gross_notional
                or projected.net_notional > self.profile.max_net_notional
            )
            allowed_deleveraging = (
                intent.reduce_only
                and projected.gross_notional <= previous.gross_notional
                and projected.net_notional <= previous.net_notional
            )
            if over_exposure and not allowed_deleveraging:
                raise PermissionError("R5.2 projected exposure exceeds the stage profile")
            reservations.append((notional, projected, previous, intent.reduce_only))
            current = projected
        return tuple(reservations)

    @staticmethod
    def _shadow_actions(result) -> list[Mapping[str, Any]]:
        output: list[Mapping[str, Any]] = []
        for reference in result.risk_cancel_order_ids:
            output.append({"operation": "WOULD_RISK_CANCEL", "reference": reference})
        for reference in result.modify_order_ids:
            output.append({"operation": "WOULD_MODIFY", "reference": reference})
        for reference in result.delete_order_ids:
            output.append({"operation": "WOULD_DELETE", "reference": reference})
        for item in result.submit_orders:
            output.append({"operation": "WOULD_SUBMIT", **R5ProductionStrategyRuntime._planner_order(item)})
        return output

    @staticmethod
    def _planner_order(item) -> Mapping[str, Any]:
        return {
            "planner_intent_id": item.intent_id,
            "symbol": item.symbol,
            "position_side": item.position_side.value,
            "order_side": item.order_side.value,
            "action": item.action.value,
            "bucket": item.bucket.value,
            "quantity": str(item.quantity),
            "price": str(item.price),
            "notional": str(item.notional),
            "reduce_only": item.reduce_only,
            "order_type": item.order_type.value,
            "time_in_force": item.time_in_force.value,
            "layer": item.layer,
            "reason": item.reason,
            "tactical_lot_id": item.tactical_lot_id,
        }

    @classmethod
    def _plan_payload(cls, result, flags: tuple[str, ...], context_diagnostics: tuple[str, ...]) -> Mapping[str, Any]:
        return {
            "ideal_orders": [cls._planner_order(item) for item in result.ideal_orders],
            "submit_orders": [cls._planner_order(item) for item in result.submit_orders],
            "cancel_order_ids": list(result.cancel_order_ids),
            "kept_order_ids": list(result.kept_order_ids),
            "modify_order_ids": list(result.modify_order_ids),
            "delete_order_ids": list(result.delete_order_ids),
            "risk_cancel_order_ids": list(result.risk_cancel_order_ids),
            "long_target_quantity": str(result.long_target_quantity),
            "short_target_quantity": str(result.short_target_quantity),
            "target_net_quantity": str(result.target_net_quantity),
            "net_gap_quantity": str(result.net_gap_quantity),
            "risk_flags": list(flags),
            "context_diagnostics": list(context_diagnostics),
            "planner_diagnostics": list(result.diagnostics),
        }

    def _validate_facts(self, facts: R5StrategyFacts, now: datetime) -> None:
        if facts.account_id != self.account_id or facts.symbol != self.symbol:
            raise PermissionError("R5.2 facts are not bound to this runtime")
        age = (now - facts.observed_at).total_seconds()
        market_age = (now - facts.market.timestamp).total_seconds()
        if age < -2 or market_age < -2:
            raise PermissionError("R5.2 facts are from the future")
        if age > self.max_fact_age_seconds or market_age > self.max_fact_age_seconds:
            raise PermissionError("R5.2 facts are stale")

    @staticmethod
    def _result_from_report(report: Mapping[str, Any]) -> R5StrategyCycleResult:
        planner = report.get("planner") if isinstance(report.get("planner"), Mapping) else {}
        return R5StrategyCycleResult(
            cycle_id=str(report["cycle_id"]),
            mode=R5StrategyMode(str(report["mode"])),
            status=str(report["status"]),
            plan_sha256=str(report["plan_sha256"]),
            writes_attempted=int(report.get("writes_attempted", 0)),
            cancel_count=sum(1 for item in report.get("actions", []) if isinstance(item, Mapping) and "CANCEL" in str(item.get("operation", ""))),
            submit_count=sum(1 for item in report.get("actions", []) if isinstance(item, Mapping) and "SUBMIT" in str(item.get("operation", ""))),
            kept_count=len(planner.get("kept_order_ids", [])) if isinstance(planner, Mapping) else 0,
            external_order_count=int(report.get("external_order_count", 0)),
            long_target_quantity=Decimal(str(report.get("long_target_quantity", "0"))),
            short_target_quantity=Decimal(str(report.get("short_target_quantity", "0"))),
            net_gap_quantity=Decimal(str(report.get("net_gap_quantity", "0"))),
            risk_flags=tuple(str(item) for item in report.get("risk_flags", [])),
            diagnostics=tuple(str(item) for item in report.get("diagnostics", [])),
            report=report,
        )
