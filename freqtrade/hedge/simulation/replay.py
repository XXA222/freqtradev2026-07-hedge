from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Iterable

from freqtrade.hedge.planning.context import (
    IntentAction,
    MarketSnapshot,
    PlanningContext,
    PlannerConfig,
    PositionSide,
    StrategyLegState,
    StrategyPlanningPort,
    TrailingPhase,
    ZERO,
)
from freqtrade.hedge.planning.ideal_orders import PureHedgePlanner
from freqtrade.hedge.planning.trailing import enter_trailing_cooldown
from freqtrade.hedge.planning.unstuck import unstuck_budget_keys
from freqtrade.hedge.integration.paper_risk_gate import apply_new_risk_gate
from freqtrade.hedge.r55.strategy import (
    StrategyDirective,
    planner_config_for_directive,
    target_net_quantity_for_directive,
)
from .cross_wallet import CrossWallet
from .exchange import (
    AccountEvent,
    AccountEventType,
    BarEvent,
    FundingEvent,
    MarketRules,
    OrderAcceptedEvent,
    OrderCancelledEvent,
    SignalEvent,
    SimulationInputEvent,
    SimulationPort,
    SimulationResult,
    StandardEvent,
)
from .funding import FundingEngine
from .matcher import ConservativeMatcher, MatchConfig
from .reports import build_report


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    config_fingerprint: str
    wallet: CrossWallet
    long_state: StrategyLegState
    short_state: StrategyLegState
    long_signal: Decimal
    short_signal: Decimal
    signal_directive: StrategyDirective
    counter: int
    symbol: str | None
    final_mark: Decimal
    last_event_key: tuple[datetime, int] | None
    processed_slots: frozenset[tuple[str, datetime, str]]
    target_net_quantity: Decimal
    net_gap_quantity: Decimal
    long_target_quantity: Decimal
    short_target_quantity: Decimal

    @property
    def signal_target_net_quantity(self) -> Decimal | None:
        """Backward-compatible alias for the signal directive exact target."""

        return self.signal_directive.target_net_quantity

    @property
    def signal_target_net_ratio(self) -> Decimal | None:
        """Backward-compatible alias for the signal directive target ratio."""

        return self.signal_directive.target_net_ratio


class EventReplayEngine(SimulationPort):
    def __init__(
        self,
        *,
        initial_balance: Decimal,
        planner_config: PlannerConfig | None = None,
        leverage: Decimal = Decimal("3"),
        fee_rate: Decimal = Decimal("0.0004"),
        long_signal: Decimal = Decimal("1"),
        short_signal: Decimal = Decimal("1"),
        target_net_quantity: Decimal | None = None,
        market_rules: MarketRules | None = None,
        planner: StrategyPlanningPort | None = None,
        match_config: MatchConfig | None = None,
    ) -> None:
        self.initial_balance = initial_balance
        self.leverage = leverage
        self.fee_rate = fee_rate
        self.planner_config = planner_config or PlannerConfig()
        self.initial_long_signal = long_signal
        self.initial_short_signal = short_signal
        self.initial_target_net_quantity_override = target_net_quantity
        self.target_net_quantity_override = target_net_quantity
        self.market_rules = market_rules or MarketRules()
        self.planner = planner or PureHedgePlanner()
        if not isinstance(self.planner, StrategyPlanningPort):
            raise TypeError("planner must implement StrategyPlanningPort")
        if match_config is None:
            self.match_config = MatchConfig(
                fee_rate=fee_rate,
                price_tick=self.market_rules.tick_size,
                qty_step=self.market_rules.qty_step,
            )
        else:
            self.match_config = replace(
                match_config,
                price_tick=self.market_rules.tick_size,
                qty_step=self.market_rules.qty_step,
            )
        self._config_fingerprint = self._build_config_fingerprint()
        self.funding = FundingEngine()
        self.matcher = ConservativeMatcher(self.match_config)
        self._reset_runtime()

    def _build_config_fingerprint(self) -> str:
        payload = repr(
            (
                self.initial_balance,
                self.leverage,
                self.fee_rate,
                self.planner_config,
                self.initial_long_signal,
                self.initial_short_signal,
                self.initial_target_net_quantity_override,
                self.market_rules,
                self.match_config,
                type(self.planner).__module__,
                type(self.planner).__qualname__,
            )
        ).encode()
        return sha256(payload).hexdigest()

    def checkpoint(self) -> ReplayCheckpoint:
        import copy

        return ReplayCheckpoint(
            config_fingerprint=self._config_fingerprint,
            wallet=copy.deepcopy(self.wallet),
            long_state=self.long_state,
            short_state=self.short_state,
            long_signal=self.long_signal,
            short_signal=self.short_signal,
            signal_directive=self.strategy_directive,
            counter=self._counter,
            symbol=self._symbol,
            final_mark=self._final_mark,
            last_event_key=self._last_event_key,
            processed_slots=frozenset(self._processed_slots),
            target_net_quantity=self._last_target_net,
            net_gap_quantity=self._last_net_gap,
            long_target_quantity=self._last_long_target,
            short_target_quantity=self._last_short_target,
        )

    def restore(self, checkpoint: ReplayCheckpoint) -> None:
        import copy

        if checkpoint.config_fingerprint != self._config_fingerprint:
            raise ValueError("checkpoint configuration does not match replay engine")
        self.wallet = copy.deepcopy(checkpoint.wallet)
        self.long_state = checkpoint.long_state
        self.short_state = checkpoint.short_state
        self.long_signal = checkpoint.long_signal
        self.short_signal = checkpoint.short_signal
        self.strategy_directive = checkpoint.signal_directive
        self.target_net_quantity_override = checkpoint.signal_directive.target_net_quantity
        self._counter = checkpoint.counter
        self._symbol = checkpoint.symbol
        self._final_mark = checkpoint.final_mark
        self._last_event_key = checkpoint.last_event_key
        self._processed_slots = set(checkpoint.processed_slots)
        self._last_target_net = checkpoint.target_net_quantity
        self._last_net_gap = checkpoint.net_gap_quantity
        self._last_long_target = checkpoint.long_target_quantity
        self._last_short_target = checkpoint.short_target_quantity

    def _reset_runtime(self) -> None:
        self.wallet = CrossWallet(
            initial_balance=self.initial_balance,
            leverage=self.leverage,
            fee_rate=self.fee_rate,
            maintenance_margin_rate=self.planner_config.maintenance_margin_rate,
            liquidation_fee_rate=self.planner_config.liquidation_fee_rate,
            liquidation_buffer_warning_ratio=(
                self.planner_config.liquidation_buffer_warning_ratio
            ),
        )
        self.long_state = StrategyLegState(side=PositionSide.LONG)
        self.short_state = StrategyLegState(side=PositionSide.SHORT)
        self.long_signal = self.initial_long_signal
        self.short_signal = self.initial_short_signal
        self.target_net_quantity_override = self.initial_target_net_quantity_override
        self.strategy_directive = StrategyDirective(
            long_score=self.initial_long_signal,
            short_score=self.initial_short_signal,
            target_net_quantity=self.initial_target_net_quantity_override,
        )
        self._counter = 0
        self._symbol: str | None = None
        self._final_mark = Decimal("1")
        self._last_event_key: tuple[datetime, int] | None = None
        self._processed_slots: set[tuple[str, datetime, str]] = set()
        self._last_target_net = ZERO
        self._last_net_gap = ZERO
        self._last_long_target = ZERO
        self._last_short_target = ZERO

    def _order_id(self, intent_id: str) -> str:
        self._counter += 1
        digest = sha256(f"{intent_id}|{self._counter}".encode()).hexdigest()[:20]
        return f"sim-{digest}"

    @staticmethod
    def _account_event_id(prefix: str, source_id: str, side: PositionSide | None) -> str:
        raw = f"{prefix}|{source_id}|{side.value if side else ''}".encode()
        return "acct-" + sha256(raw).hexdigest()[:24]

    def _assert_symbol(self, symbol: str) -> None:
        if self._symbol is None:
            self._symbol = symbol
        elif symbol != self._symbol:
            raise ValueError(
                "EventReplayEngine is single-symbol; create one engine per symbol "
                f"({self._symbol!r} != {symbol!r})"
            )

    def _plan(self, bar: BarEvent) -> tuple[StandardEvent, ...]:
        if self.wallet.liquidated:
            return ()
        rules = self.market_rules
        market = MarketSnapshot(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            bid=bar.open,
            ask=bar.open,
            mark=bar.open,
            tick_size=rules.tick_size,
            qty_step=rules.qty_step,
            min_qty=rules.min_qty,
            min_notional=rules.min_notional,
        )
        wallet_snapshot = self.wallet.planner_snapshot(bar.open, bar.timestamp)
        effective_config = planner_config_for_directive(self.planner_config, self.strategy_directive)
        effective_target = target_net_quantity_for_directive(
            directive=self.strategy_directive, base=self.planner_config,
            equity=wallet_snapshot.equity, mark_price=bar.open,
        )
        context = PlanningContext(
            market=market, wallet=wallet_snapshot, config=effective_config,
            long_state=self.long_state, short_state=self.short_state,
            long_signal=self.strategy_directive.long_score,
            short_signal=self.strategy_directive.short_score,
            target_net_quantity=effective_target,
        )
        result = self.planner.plan(context)
        result, _ = apply_new_risk_gate(
            result, enabled=self.strategy_directive.allow_new_risk,
            current_long_state=self.long_state, current_short_state=self.short_state,
        )
        self.long_state = result.long_state
        self.short_state = result.short_state
        self._last_target_net = result.target_net_quantity
        self._last_net_gap = result.net_gap_quantity
        self._last_long_target = result.long_target_quantity
        self._last_short_target = result.short_target_quantity
        emitted: list[StandardEvent] = []
        for order_id in result.cancel_order_ids:
            self.wallet.cancel_order(order_id)
            emitted.append(OrderCancelledEvent(timestamp=bar.timestamp, order_id=order_id))
        for intent in result.submit_orders:
            order_id = self._order_id(intent.intent_id)
            self.wallet.accept_order(order_id, intent, accepted_at=bar.timestamp)
            emitted.append(
                OrderAcceptedEvent(
                    timestamp=bar.timestamp,
                    order_id=order_id,
                    intent=intent,
                )
            )
        return tuple(emitted)

    def _advance_states_after_fills(self, bar: BarEvent, fills) -> None:
        touched_entries: dict[PositionSide, set[int]] = {
            PositionSide.LONG: set(),
            PositionSide.SHORT: set(),
        }
        touched_reductions: dict[PositionSide, set[int]] = {
            PositionSide.LONG: set(),
            PositionSide.SHORT: set(),
        }
        any_entry: set[PositionSide] = set()
        any_reduction: set[PositionSide] = set()
        unstuck_losses: dict[PositionSide, Decimal] = {
            PositionSide.LONG: ZERO,
            PositionSide.SHORT: ZERO,
        }
        unstuck_sides: set[PositionSide] = set()
        for fill in fills:
            if fill.reduce_only:
                touched_reductions[fill.position_side].add(fill.layer)
                any_reduction.add(fill.position_side)
            else:
                touched_entries[fill.position_side].add(fill.layer)
                any_entry.add(fill.position_side)
            if fill.action is IntentAction.UNSTUCK:
                unstuck_sides.add(fill.position_side)
                realized = self.wallet.realized_by_fill.get(fill.event_id, ZERO)
                unstuck_losses[fill.position_side] += max(-realized, ZERO) + fill.fee

        def layer_is_complete(side: PositionSide, layer: int, reduce_only: bool) -> bool:
            return not any(
                intent.position_side is side
                and intent.layer == layer
                and intent.reduce_only == reduce_only
                for intent, _ in self.wallet.active_orders.values()
            )

        def next_state(side: PositionSide, state: StrategyLegState) -> StrategyLegState:
            completed_entries = sum(
                1
                for layer in touched_entries[side]
                if layer_is_complete(side, layer, False)
            )
            completed_reductions = sum(
                1
                for layer in touched_reductions[side]
                if layer_is_complete(side, layer, True)
            )
            layer_count = min(
                state.grid_layers_filled + completed_entries,
                self.planner_config.max_grid_layers,
            )
            layer_count = max(layer_count - completed_reductions, 0)
            next_value = replace(state, grid_layers_filled=layer_count)
            if side in any_entry:
                next_value = enter_trailing_cooldown(
                    replace(next_value, last_entry_at=bar.timestamp),
                    timestamp=bar.timestamp,
                    cooldown_seconds=self.planner_config.cooldown_seconds,
                )
            if side in any_reduction:
                next_value = replace(next_value, last_reduce_at=bar.timestamp)
            if side in unstuck_sides:
                day_key, week_key = unstuck_budget_keys(bar.timestamp)
                daily = (
                    next_value.unstuck_daily_loss
                    if next_value.unstuck_budget_day == day_key
                    else ZERO
                )
                weekly = (
                    next_value.unstuck_weekly_loss
                    if next_value.unstuck_budget_week == week_key
                    else ZERO
                )
                next_value = replace(
                    next_value,
                    last_unstuck_at=bar.timestamp,
                    unstuck_budget_day=day_key,
                    unstuck_budget_week=week_key,
                    unstuck_daily_loss=daily + unstuck_losses[side],
                    unstuck_weekly_loss=weekly + unstuck_losses[side],
                )
            if self.wallet.leg(side).quantity == 0:
                next_value = replace(
                    next_value,
                    grid_layers_filled=0,
                    trailing_phase=TrailingPhase.IDLE,
                    trailing_trigger_price=None,
                    trailing_extreme=None,
                    trailing_started_at=None,
                    trailing_confirmed_at=None,
                    trailing_cooldown_until=None,
                    trailing_armed=False,
                )
            return next_value

        self.long_state = next_state(PositionSide.LONG, self.long_state)
        self.short_state = next_state(PositionSide.SHORT, self.short_state)

    @staticmethod
    def _event_priority(event: SimulationInputEvent) -> int:
        if isinstance(event, SignalEvent):
            return 0
        if isinstance(event, FundingEvent):
            return 1
        return 2

    @staticmethod
    def _input_slot(event: SimulationInputEvent) -> tuple[str, datetime, str]:
        if isinstance(event, SignalEvent):
            kind = "SIGNAL"
        elif isinstance(event, FundingEvent):
            kind = "FUNDING"
        else:
            kind = "BAR"
        return kind, event.timestamp, event.symbol

    def _report(self):
        return build_report(
            self.wallet,
            self._final_mark,
            target_net_quantity=self._last_target_net,
            net_gap_quantity=self._last_net_gap,
            long_target_quantity=self._last_long_target,
            short_target_quantity=self._last_short_target,
        )

    def _run(
        self,
        events: Iterable[SimulationInputEvent],
        *,
        reset: bool,
    ) -> SimulationResult:
        if reset:
            self._reset_runtime()
        emitted: list[StandardEvent] = []
        snapshots = []
        indexed_events = list(enumerate(events))
        indexed_events.sort(
            key=lambda item: (
                item[1].timestamp,
                self._event_priority(item[1]),
                item[0],
            )
        )

        prospective_symbol = self._symbol
        prospective_last_key = self._last_event_key
        prospective_slots = set(self._processed_slots)
        for _, event in indexed_events:
            event_key = (event.timestamp, self._event_priority(event))
            if prospective_last_key is not None and event_key < prospective_last_key:
                raise ValueError(
                    "incremental simulation events cannot move backwards in time or priority"
                )
            slot = self._input_slot(event)
            if slot in prospective_slots:
                raise ValueError(
                    f"duplicate standard event slot: {slot[0]} {event.symbol} "
                    f"at {event.timestamp.isoformat()}"
                )
            if prospective_symbol is None:
                prospective_symbol = event.symbol
            elif event.symbol != prospective_symbol:
                raise ValueError(
                    "EventReplayEngine is single-symbol; create one engine per symbol "
                    f"({prospective_symbol!r} != {event.symbol!r})"
                )
            prospective_slots.add(slot)
            prospective_last_key = event_key

        for _, event in indexed_events:
            event_key = (event.timestamp, self._event_priority(event))
            slot = self._input_slot(event)
            self._processed_slots.add(slot)
            self._last_event_key = event_key
            self._assert_symbol(event.symbol)
            if isinstance(event, SignalEvent):
                self.long_signal = event.long_signal
                self.short_signal = event.short_signal
                self.target_net_quantity_override = event.target_net
                self.strategy_directive = StrategyDirective(
                    long_score=event.long_signal, short_score=event.short_signal,
                    target_net_quantity=(None if event.target_net_ratio is not None else event.target_net),
                    target_net_ratio=event.target_net_ratio, confidence=event.confidence,
                    risk_scale=event.risk_scale, long_exposure_scale=event.long_exposure_scale,
                    short_exposure_scale=event.short_exposure_scale, allow_new_risk=event.allow_new_risk,
                    regime=event.regime, reason=event.reason, model_version=event.model_version,
                )
                emitted.append(event)
                continue
            if isinstance(event, FundingEvent):
                before_long = self.wallet.long_funding
                before_short = self.wallet.short_funding
                self.funding.apply(self.wallet, event)
                emitted.append(event)
                for side, amount in (
                    (PositionSide.LONG, self.wallet.long_funding - before_long),
                    (PositionSide.SHORT, self.wallet.short_funding - before_short),
                ):
                    if amount != ZERO:
                        emitted.append(
                            AccountEvent(
                                event_id=self._account_event_id(
                                    "funding",
                                    event.timestamp.isoformat(),
                                    side,
                                ),
                                timestamp=event.timestamp,
                                symbol=event.symbol,
                                event_type=AccountEventType.FUNDING,
                                amount=amount,
                                position_side=side,
                                source_event_id=event.timestamp.isoformat(),
                                description="funding settlement",
                            )
                        )
                self._final_mark = event.mark_price
                snapshots.append(self.wallet.snapshot(event.timestamp, self._final_mark))
                continue

            emitted.append(event)
            # Existing orders are matched first.  The current analyzed candle
            # may produce a new signal and a new ideal order set at its close,
            # but those orders are not allowed to use the same candle's path.
            # This is the shared no-lookahead contract for Backtest and Paper.
            if not self.wallet.liquidated:
                outcome = self.matcher.match_outcome(event, self.wallet)
                for fill in outcome.fills:
                    self.wallet.apply_fill(fill)
                    emitted.append(fill)
                    if fill.fee != ZERO:
                        emitted.append(
                            AccountEvent(
                                event_id=self._account_event_id(
                                    "fee",
                                    fill.event_id,
                                    fill.position_side,
                                ),
                                timestamp=fill.timestamp,
                                symbol=fill.symbol,
                                event_type=AccountEventType.FEE,
                                amount=-fill.fee,
                                position_side=fill.position_side,
                                source_event_id=fill.event_id,
                                description=f"{fill.liquidity_role.value.lower()} fee",
                            )
                        )
                if outcome.liquidation_event is not None:
                    liquidation_cancel_ids = tuple(sorted(self.wallet.active_orders))
                    self.wallet.apply_liquidation(outcome.liquidation_event)
                    for order_id in liquidation_cancel_ids:
                        emitted.append(
                            OrderCancelledEvent(
                                timestamp=outcome.liquidation_event.timestamp,
                                order_id=order_id,
                            )
                        )
                    emitted.append(outcome.liquidation_event)
                    emitted.append(
                        AccountEvent(
                            event_id=self._account_event_id(
                                "liquidation-pnl",
                                outcome.liquidation_event.event_id,
                                None,
                            ),
                            timestamp=outcome.liquidation_event.timestamp,
                            symbol=outcome.liquidation_event.symbol,
                            event_type=AccountEventType.LIQUIDATION,
                            amount=outcome.liquidation_event.realized_pnl,
                            source_event_id=outcome.liquidation_event.event_id,
                            description="cross account liquidation realized PnL",
                        )
                    )
                    if outcome.liquidation_event.fee != ZERO:
                        emitted.append(
                            AccountEvent(
                                event_id=self._account_event_id(
                                    "liquidation-fee",
                                    outcome.liquidation_event.event_id,
                                    None,
                                ),
                                timestamp=outcome.liquidation_event.timestamp,
                                symbol=outcome.liquidation_event.symbol,
                                event_type=AccountEventType.FEE,
                                amount=-outcome.liquidation_event.fee,
                                source_event_id=outcome.liquidation_event.event_id,
                                description="cross account liquidation fee",
                            )
                        )
                for order_id in outcome.expired_order_ids:
                    if self.wallet.remaining(order_id) > 0:
                        self.wallet.cancel_order(order_id)
                        emitted.append(
                            OrderCancelledEvent(timestamp=event.timestamp, order_id=order_id)
                        )
                self.wallet.merge_risk_metrics(
                    gross_peak=outcome.gross_peak,
                    equity_peak=outcome.equity_peak,
                    max_drawdown=outcome.max_drawdown,
                )
                self._advance_states_after_fills(event, outcome.fills)
            if not self.wallet.liquidated:
                emitted.extend(self._plan(event))
            self._final_mark = event.close
            snapshots.append(self.wallet.snapshot(event.timestamp, self._final_mark))

        return SimulationResult(
            events=tuple(emitted),
            snapshots=tuple(snapshots),
            report=self._report(),
        )

    def replay(self, events: Iterable[SimulationInputEvent]) -> SimulationResult:
        """Run a deterministic batch simulation from a clean initial state."""
        return self._run(events, reset=True)

    def advance(self, events: Iterable[SimulationInputEvent]) -> SimulationResult:
        """Advance atomically; any failure restores the pre-batch simulation state."""
        checkpoint = self.checkpoint()
        try:
            return self._run(events, reset=False)
        except Exception:
            self.restore(checkpoint)
            raise
