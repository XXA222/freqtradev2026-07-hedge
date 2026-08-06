"""Binance income -> authoritative SQL daily-loss projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable, Mapping

from .models import R5LimitProfile
from .sql_budget import SqlR5DailyBudgetStore


@dataclass(frozen=True, slots=True)
class R5IncomeProjectionReport:
    inspected: int
    applied: int
    replayed: int
    realized_loss: Decimal


class R5IncomeProjector:
    """Project immutable funding/commission/realized-PnL facts exactly once."""

    _LOSS_RELEVANT = frozenset({"FUNDING_FEE", "COMMISSION", "REALIZED_PNL"})

    def __init__(
        self,
        *,
        budget: SqlR5DailyBudgetStore,
        profile: R5LimitProfile,
    ) -> None:
        self._budget = budget
        self._profile = profile

    def project(
        self,
        *,
        account_id: str,
        rows: Iterable[Mapping[str, object]],
    ) -> R5IncomeProjectionReport:
        inspected = applied = replayed = 0
        for row in rows:
            inspected += 1
            kind = str(row.get("incomeType", "")).strip().upper()
            if kind not in self._LOSS_RELEVANT:
                continue
            external_id = str(row.get("tranId", row.get("tradeId", ""))).strip()
            timestamp = int(row.get("time", 0) or 0)
            if not external_id or timestamp <= 0:
                raise ValueError("Binance income row has no stable identity/time")
            amount = Decimal(str(row.get("income", "0")))
            was_applied, _ = self._budget.record_income_event(
                account_id=account_id,
                external_event_id=external_id,
                income_type=kind,
                amount=amount,
                occurred_at=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                profile=self._profile,
                symbol=(None if row.get("symbol") in (None, "") else str(row.get("symbol"))),
                asset=(None if row.get("asset") in (None, "") else str(row.get("asset"))),
                payload=row,
            )
            if was_applied:
                applied += 1
            else:
                replayed += 1
        return R5IncomeProjectionReport(
            inspected,
            applied,
            replayed,
            self._budget.authoritative_realized_loss(account_id),
        )
