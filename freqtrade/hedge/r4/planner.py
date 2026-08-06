from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from freqtrade.hedge.planning.context import (
    OrderIntent,
    OrderType,
    PlannerConfig,
    PlanningContext,
    PlanningResult,
    PositionSide,
    TimeInForce,
    ZERO,
)
from freqtrade.hedge.planning.ideal_orders import PureHedgePlanner


@dataclass(frozen=True, slots=True)
class R4PlanningInputs:
    context: PlanningContext
    realized_volatility: Decimal = ZERO
    funding_rate: Decimal = ZERO
    funding_guard_threshold: Decimal = Decimal("0.001")
    volatility_spacing_multiplier: Decimal = Decimal("1.5")
    max_single_order_notional_cap: Decimal | None = None
    max_gross_wallet_exposure_cap: Decimal | None = None

    def __post_init__(self) -> None:
        values = (
            self.realized_volatility,
            self.funding_rate,
            self.funding_guard_threshold,
            self.volatility_spacing_multiplier,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("R4 planning inputs must be finite")
        if self.realized_volatility < ZERO or self.funding_guard_threshold < ZERO:
            raise ValueError("volatility and funding guard cannot be negative")
        if self.volatility_spacing_multiplier < ZERO:
            raise ValueError("volatility spacing multiplier cannot be negative")
        for name, cap in (
            ("max_single_order_notional_cap", self.max_single_order_notional_cap),
            ("max_gross_wallet_exposure_cap", self.max_gross_wallet_exposure_cap),
        ):
            if cap is not None and (not cap.is_finite() or cap <= ZERO):
                raise ValueError(f"{name} must be finite and positive")
        if self.max_gross_wallet_exposure_cap is not None and self.max_gross_wallet_exposure_cap > 1:
            raise ValueError("max_gross_wallet_exposure_cap cannot exceed one")


@dataclass(frozen=True, slots=True)
class R4PlanningDecision:
    result: PlanningResult
    effective_config: PlannerConfig
    risk_flags: tuple[str, ...]


class R4HedgePlanner:
    """Final R4 policy layer over the shared deterministic planner."""

    def __init__(self, planner: PureHedgePlanner | None = None) -> None:
        self.planner = planner or PureHedgePlanner()

    def plan(self, inputs: R4PlanningInputs) -> R4PlanningDecision:
        context = inputs.context
        spacing = max(
            context.config.grid_spacing,
            inputs.realized_volatility * inputs.volatility_spacing_multiplier,
        )
        existing_notional_cap = (
            context.config.max_single_order_notional
            if context.config.max_single_order_notional > ZERO
            else Decimal("25")
        )
        notional_cap = (
            min(existing_notional_cap, inputs.max_single_order_notional_cap)
            if inputs.max_single_order_notional_cap is not None
            else existing_notional_cap
        )
        gross_cap = (
            min(context.config.max_gross_wallet_exposure, inputs.max_gross_wallet_exposure_cap)
            if inputs.max_gross_wallet_exposure_cap is not None
            else context.config.max_gross_wallet_exposure
        )
        config = replace(
            context.config,
            grid_spacing=spacing,
            max_single_order_notional=notional_cap,
            max_gross_wallet_exposure=gross_cap,
            max_wallet_exposure_long=min(context.config.max_wallet_exposure_long, gross_cap),
            max_wallet_exposure_short=min(context.config.max_wallet_exposure_short, gross_cap),
            unstuck_limit_only=True,
        )
        long_signal = context.long_signal
        short_signal = context.short_signal
        flags: list[str] = []
        if inputs.funding_rate > inputs.funding_guard_threshold:
            long_signal = ZERO
            flags.append("LONG_ENTRY_SUPPRESSED_BY_POSITIVE_FUNDING")
        elif inputs.funding_rate < -inputs.funding_guard_threshold:
            short_signal = ZERO
            flags.append("SHORT_ENTRY_SUPPRESSED_BY_NEGATIVE_FUNDING")
        result = self.planner.plan(
            replace(
                context,
                config=config,
                long_signal=long_signal,
                short_signal=short_signal,
            )
        )
        suppressed_sides: set[PositionSide] = set()
        if inputs.funding_rate > inputs.funding_guard_threshold:
            suppressed_sides.add(PositionSide.LONG)
        elif inputs.funding_rate < -inputs.funding_guard_threshold:
            suppressed_sides.add(PositionSide.SHORT)
        if suppressed_sides:
            result = replace(
                result,
                ideal_orders=tuple(
                    item
                    for item in result.ideal_orders
                    if item.reduce_only or item.position_side not in suppressed_sides
                ),
                submit_orders=tuple(
                    item
                    for item in result.submit_orders
                    if item.reduce_only or item.position_side not in suppressed_sides
                ),
            )
        flags.extend(validate_planning_invariants(context, result, config=config))
        return R4PlanningDecision(result=result, effective_config=config, risk_flags=tuple(flags))


def _validate_order(
    order: OrderIntent,
    *,
    context: PlanningContext,
    config: PlannerConfig,
) -> None:
    if order.order_type is not OrderType.LIMIT:
        raise ValueError("R4 planner permits LIMIT orders only")
    if order.time_in_force is not TimeInForce.GTC:
        raise ValueError("R4 planner permits GTC planning intents only")
    if order.symbol != context.market.symbol:
        raise ValueError("planner order symbol does not match context")
    if config.max_single_order_notional > ZERO and order.notional > config.max_single_order_notional:
        raise ValueError("planner order exceeds maximum single-order notional")
    if order.quantity % context.market.qty_step != ZERO:
        raise ValueError("planner order quantity is not aligned to quantity step")
    if order.price % context.market.tick_size != ZERO:
        raise ValueError("planner order price is not aligned to tick size")
    if order.quantity < context.market.min_qty:
        raise ValueError("planner order is below minimum quantity")
    if order.notional < context.market.min_notional:
        raise ValueError("planner order is below minimum notional")


def validate_planning_invariants(
    context: PlanningContext,
    result: PlanningResult,
    *,
    config: PlannerConfig | None = None,
) -> tuple[str, ...]:
    cfg = config or context.config
    flags: list[str] = []
    ideal_by_id: dict[str, OrderIntent] = {}
    reduce_by_side = {PositionSide.LONG: ZERO, PositionSide.SHORT: ZERO}
    for order in result.ideal_orders:
        if order.intent_id in ideal_by_id:
            raise ValueError("planner produced duplicate ideal intent_id")
        ideal_by_id[order.intent_id] = order
        _validate_order(order, context=context, config=cfg)
        if order.reduce_only:
            reduce_by_side[order.position_side] += order.quantity
    submit_ids: set[str] = set()
    for order in result.submit_orders:
        if order.intent_id in submit_ids:
            raise ValueError("planner produced duplicate submit intent_id")
        submit_ids.add(order.intent_id)
        _validate_order(order, context=context, config=cfg)
        ideal = ideal_by_id.get(order.intent_id)
        if ideal is None or ideal != order:
            raise ValueError("submit order is not an exact member of the ideal order set")
    if len(result.cancel_order_ids) != len(set(result.cancel_order_ids)):
        raise ValueError("planner produced duplicate cancel order IDs")
    if set(result.cancel_order_ids) & set(result.kept_order_ids):
        raise ValueError("an order cannot be both canceled and kept")
    for side in (PositionSide.LONG, PositionSide.SHORT):
        open_qty = context.wallet.leg(side).quantity
        if reduce_by_side[side] > open_qty:
            raise ValueError(f"{side.value} reduce intents exceed open position")
    actual_net = context.wallet.long.quantity - context.wallet.short.quantity
    if result.net_gap_quantity != result.target_net_quantity - actual_net:
        flags.append("NET_GAP_REPORTED_BY_PLANNER_POLICY")
    return tuple(flags)
