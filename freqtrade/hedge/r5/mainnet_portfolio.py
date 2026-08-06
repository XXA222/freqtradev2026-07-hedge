"""Translate live Binance facts into the official account-wide risk portfolio.

Ownership is determined from durable SQL order membership, never from symbol alone.
All external orders still contribute pending risk, but only managed orders are eligible
for cancellation or cancel-replace.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from freqtrade.enums.hedge import PositionAction, PositionSide
from freqtrade.hedge.execution.ownership import (
    ExecutionOrderOwnershipRegistry,
    OrderOwnership,
)
from freqtrade.hedge.risk.models import PendingOrderRisk
from freqtrade.hedge.risk.portfolio import (
    PositionRiskLeg,
    RiskPortfolioSnapshot,
    build_risk_portfolio,
)

from .mainnet_api import R5LivePreflight


@dataclass(frozen=True, slots=True)
class R5PortfolioEvidence:
    portfolio: RiskPortfolioSnapshot
    managed_client_order_ids: tuple[str, ...]
    external_client_order_ids: tuple[str, ...]
    orphan_prefix_client_order_ids: tuple[str, ...]
    unmanaged_position_legs: tuple[str, ...]


def _decimal(value: object, *, default: str = "0") -> Decimal:
    return Decimal(str(default if value in (None, "") else value))


def _order_action(order: Mapping[str, str]) -> PositionAction:
    reduce_only = str(order.get("reduce_only", "false")).lower() == "true"
    return PositionAction.REDUCE if reduce_only else PositionAction.INCREASE


def build_live_risk_portfolio(
    *,
    preflight: R5LivePreflight,
    ownership: ExecutionOrderOwnershipRegistry,
    adopted_position_legs: tuple[str, ...] = (),
    maintenance_margin_rate: Decimal = Decimal("0.01"),
) -> R5PortfolioEvidence:
    account_id = preflight.account.account_id
    adopted = {str(item).strip().upper() for item in adopted_position_legs if str(item).strip()}
    position_items: list[PositionRiskLeg] = []
    unmanaged_legs: list[str] = []
    for row in preflight.managed_position_legs:
        symbol = str(row.get("symbol", "")).upper()
        side = PositionSide(str(row.get("position_side", "")).upper())
        quantity = abs(_decimal(row.get("position_amount")))
        if quantity == 0:
            continue
        mark = _decimal(row.get("mark_price"))
        leverage = _decimal(row.get("leverage"), default=str(preflight.leverage_by_symbol.get(symbol, Decimal("1"))))
        maintenance = _decimal(row.get("maintenance_margin"))
        if maintenance <= 0:
            maintenance = quantity * mark * maintenance_margin_rate
        liquidation = _decimal(row.get("liquidation_price"))
        key = f"{symbol}:{side.value}"
        if key not in adopted:
            unmanaged_legs.append(key)
        position_items.append(
            PositionRiskLeg(
                account_id=account_id,
                symbol=symbol,
                position_side=side,
                quantity=quantity,
                mark_price=mark,
                leverage=max(Decimal("1"), leverage),
                reported_initial_margin=(
                    None if _decimal(row.get("initial_margin")) <= 0 else _decimal(row.get("initial_margin"))
                ),
                maintenance_margin=maintenance,
                liquidation_price=None if liquidation <= 0 else liquidation,
            )
        )

    pending: list[PendingOrderRisk] = []
    managed: list[str] = []
    external: list[str] = []
    orphan: list[str] = []
    for row in preflight.active_order_summaries:
        client_id = str(row.get("client_order_id", "")).strip()
        if not client_id:
            # Binance orders should always contain a client id; fail closed for ownership.
            client_id = f"MISSING:{row.get('order_id', '')}"
            external.append(client_id)
        else:
            decision = ownership.classify(client_id)
            if decision.ownership is OrderOwnership.MANAGED:
                managed.append(client_id)
            elif decision.ownership is OrderOwnership.ORPHAN_PREFIX:
                orphan.append(client_id)
            else:
                external.append(client_id)
        symbol = str(row.get("symbol", "")).upper()
        side_value = str(row.get("position_side", "")).upper()
        if side_value not in {"LONG", "SHORT"}:
            raise PermissionError(f"R5 open order has invalid hedge side: {client_id}")
        original = _decimal(row.get("original_quantity"))
        executed = _decimal(row.get("executed_quantity"))
        remaining = max(Decimal("0"), original - executed)
        if remaining == 0:
            continue
        price = _decimal(row.get("price"))
        if price <= 0:
            book = preflight.books.get(symbol)
            if book is None:
                raise PermissionError(f"R5 cannot price pending order: {client_id}")
            price = book.mid
        pending.append(
            PendingOrderRisk(
                account_id=account_id,
                symbol=symbol,
                position_side=PositionSide(side_value),
                action=_order_action(row),
                remaining_quantity=remaining,
                reference_price=price,
                leverage=max(Decimal("1"), preflight.leverage_by_symbol.get(symbol, Decimal("1"))),
                maintenance_margin_rate=maintenance_margin_rate,
            )
        )

    portfolio = build_risk_portfolio(
        account_id=account_id,
        equity=preflight.account.equity,
        wallet_balance=preflight.wallet_balance,
        available_balance=preflight.available_balance,
        positions=position_items,
        pending_orders=pending,
        risk_data_valid=not unmanaged_legs,
        observed_at_ms=int(preflight.account.observed_at.timestamp() * 1000),
        exchange="binance",
        strict_completeness=False,
    )
    return R5PortfolioEvidence(
        portfolio=portfolio,
        managed_client_order_ids=tuple(sorted(set(managed))),
        external_client_order_ids=tuple(sorted(set(external))),
        orphan_prefix_client_order_ids=tuple(sorted(set(orphan))),
        unmanaged_position_legs=tuple(sorted(set(unmanaged_legs))),
    )
