"""Control-plane adapter sharing the same authoritative R5 order ledger/runtime."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

from freqtrade.hedge.risk.portfolio import RiskPortfolioSnapshot
from freqtrade.hedge.execution.service import OrderIntent

from .arming import R5StagePermit
from .authoritative_runtime import R5AuthoritativeBatchReport, R5AuthoritativeRuntime
from .budget import R5BudgetPort
from .models import R5AccountSnapshot, R5LimitProfile
from .emergency import R5EmergencyExitPermit


@dataclass(frozen=True, slots=True)
class R5AuthoritativeControlAdapter:
    runtime: R5AuthoritativeRuntime

    def kill(self, reason: str) -> None:
        text = str(reason).strip()
        if not text:
            raise ValueError("kill-switch reason is required")
        self.runtime.kill_switch.halt(text)

    def cancel_managed(
        self,
        *,
        references: Iterable[str],
        portfolio: RiskPortfolioSnapshot,
        permit: R5StagePermit,
        profile: R5LimitProfile,
        budget: R5BudgetPort | None = None,
    ) -> R5AuthoritativeBatchReport:
        return self.runtime.execute_batch(
            portfolio=portfolio,
            intents=(),
            cancel_references=tuple(references),
            permit=permit,
            profile=profile,
            budget=budget,
            durable_namespace="r5-control-cancel",
        )
    def emergency_reduce(
        self,
        *,
        intent: OrderIntent,
        portfolio: RiskPortfolioSnapshot,
        emergency_permit: R5EmergencyExitPermit,
        stage_permit: R5StagePermit,
        profile: R5LimitProfile,
        budget: R5BudgetPort | None = None,
        budget_reservations: Sequence[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ] = (),
    ) -> R5AuthoritativeBatchReport:
        return self.runtime.execute_emergency_reduce(
            portfolio=portfolio,
            intent=intent,
            emergency_permit=emergency_permit,
            stage_permit=stage_permit,
            profile=profile,
            budget=budget,
            budget_reservations=budget_reservations,
        )

