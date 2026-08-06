"""Strict JSON boundary for durable R4/R5 PlanningResult evidence."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from freqtrade.hedge.planning.context import (
    IntentAction,
    OrderIntent,
    OrderSide,
    OrderType,
    PlanningResult,
    PositionBucket,
    PositionSide,
    StrategyLegState,
    TimeInForce,
)


def _intent(value: Mapping[str, Any]) -> OrderIntent:
    return OrderIntent(
        intent_id=str(value["planner_intent_id"] if "planner_intent_id" in value else value["intent_id"]),
        symbol=str(value["symbol"]).upper(),
        position_side=PositionSide(str(value["position_side"]).upper()),
        order_side=OrderSide(str(value.get("order_side", value.get("side", ""))).upper()),
        action=IntentAction(str(value["action"]).upper()),
        bucket=PositionBucket(str(value.get("bucket", "CORE")).upper()),
        quantity=Decimal(str(value["quantity"])),
        price=Decimal(str(value["price"])),
        reduce_only=bool(value.get("reduce_only", False)),
        order_type=OrderType(str(value.get("order_type", "LIMIT")).upper()),
        time_in_force=TimeInForce(str(value.get("time_in_force", "GTC")).upper()),
        layer=int(value.get("layer", 0)),
        reason=str(value.get("reason", "")),
        tactical_lot_id=(
            None if value.get("tactical_lot_id") in (None, "") else str(value["tactical_lot_id"])
        ),
    )


def planning_result_from_mapping(value: Mapping[str, Any]) -> PlanningResult:
    planner = value.get("planner", value)
    if not isinstance(planner, Mapping):
        raise ValueError("planning payload must contain a planner object")
    ideal_raw = planner.get("ideal_orders", [])
    submit_raw = planner.get("submit_orders", [])
    if not isinstance(ideal_raw, list) or not isinstance(submit_raw, list):
        raise ValueError("planning order collections must be lists")
    ideal = tuple(_intent(item) for item in ideal_raw if isinstance(item, Mapping))
    submit = tuple(_intent(item) for item in submit_raw if isinstance(item, Mapping))
    if len(ideal) != len(ideal_raw) or len(submit) != len(submit_raw):
        raise ValueError("planning order entries must be objects")
    return PlanningResult(
        ideal_orders=ideal,
        submit_orders=submit,
        cancel_order_ids=tuple(str(item) for item in planner.get("cancel_order_ids", [])),
        kept_order_ids=tuple(str(item) for item in planner.get("kept_order_ids", [])),
        long_state=StrategyLegState(PositionSide.LONG),
        short_state=StrategyLegState(PositionSide.SHORT),
        diagnostics=tuple(str(item) for item in planner.get("planner_diagnostics", planner.get("diagnostics", []))),
        modify_order_ids=tuple(str(item) for item in planner.get("modify_order_ids", [])),
        delete_order_ids=tuple(str(item) for item in planner.get("delete_order_ids", [])),
        risk_cancel_order_ids=tuple(str(item) for item in planner.get("risk_cancel_order_ids", [])),
        target_net_quantity=Decimal(str(planner.get("target_net_quantity", "0"))),
        net_gap_quantity=Decimal(str(planner.get("net_gap_quantity", "0"))),
        long_target_quantity=Decimal(str(planner.get("long_target_quantity", "0"))),
        short_target_quantity=Decimal(str(planner.get("short_target_quantity", "0"))),
    )


def load_planning_result(path: str | Path, *, symbol: str | None = None) -> tuple[str, PlanningResult]:
    resolved = Path(path).expanduser().resolve(strict=True)
    payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("planning report must contain a JSON object")
    if isinstance(payload.get("cycles"), list):
        cycles = [item for item in payload["cycles"] if isinstance(item, Mapping)]
        if symbol is not None:
            cycles = [item for item in cycles if str(item.get("symbol", "")).upper() == symbol.upper()]
        if len(cycles) != 1:
            raise ValueError("planning report must select exactly one cycle")
        selected = cycles[0]
    else:
        selected = payload
    cycle_id = str(selected.get("cycle_id", selected.get("run_id", ""))).strip()
    if not cycle_id:
        raise ValueError("planning report has no cycle_id")
    return cycle_id, planning_result_from_mapping(selected)
