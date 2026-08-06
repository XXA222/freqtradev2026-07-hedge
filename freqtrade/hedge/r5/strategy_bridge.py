from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable, Mapping, Protocol

from freqtrade.hedge.execution.service import OrderIntent, OrderType

from .arming import R5StagePermit
from .budget import R5DailyBudgetStore
from .executor import R5ExecutorPort
from .models import R5AccountSnapshot, R5LimitProfile, R5Stage


class R5StrategyPort(Protocol):
    def __call__(
        self,
        *,
        cycle_index: int,
        account: R5AccountSnapshot,
        profile: R5LimitProfile,
        stage: R5Stage,
    ) -> Iterable[OrderIntent]: ...


@dataclass(slots=True)
class R5RestrictedAutoBridge:
    """Executes an injected planner through the same limits and execution adapter.

    The bridge accepts only LIMIT intents for the bound account and current R5 stage.
    It does not generate strategy decisions itself and cannot enlarge the configured
    profile. Every exchange write consumes the process-local stage permit.
    """

    stage: R5Stage
    profile: R5LimitProfile
    account_id: str
    strategy: R5StrategyPort
    executor: R5ExecutorPort
    permit: R5StagePermit
    budget: R5DailyBudgetStore

    def cycle(self, index: int, account: R5AccountSnapshot) -> Mapping[str, object]:
        intents = tuple(
            self.strategy(
                cycle_index=index,
                account=account,
                profile=self.profile,
                stage=self.stage,
            )
        )
        if len(intents) > self.profile.max_open_orders:
            raise PermissionError("R5 strategy emitted too many intents in one cycle")
        self.permit.preflight(len(intents))

        prepared: list[tuple[OrderIntent, Decimal, R5AccountSnapshot, R5AccountSnapshot]] = []
        projected_account = account
        for intent in intents:
            if not isinstance(intent, OrderIntent):
                raise TypeError("R5 strategy must return OrderIntent values")
            if intent.account_id != self.account_id:
                raise PermissionError("R5 strategy intent account mismatch")
            if intent.symbol not in {"BTCUSDT", "ETHUSDT"}:
                raise PermissionError("R5 strategy intent symbol is not allowlisted")
            if intent.order_type is not OrderType.LIMIT:
                raise PermissionError("R5 strategy cannot submit MARKET orders")
            if intent.limit_price is None:
                raise PermissionError("R5 strategy LIMIT intent has no price")
            notional = intent.quantity * intent.limit_price
            previous = projected_account
            projected_account = self._project(previous, intent, notional)
            prepared.append((intent, notional, projected_account, previous))

        # Reserve the whole batch under one transaction before the first exchange write.
        self.budget.reserve_orders(
            account_id=self.account_id,
            profile=self.profile,
            reservations=tuple(
                (notional, projected, previous, intent.action.reduces_risk)
                for intent, notional, projected, previous in prepared
            ),
        )

        outcomes: list[dict[str, object]] = []
        writes = 0
        for intent, _notional, _projected, _previous in prepared:
            self.permit.consume("AUTO_SUBMIT")
            result = self.executor.submit(intent)
            writes += 1
            outcomes.append(
                {
                    "client_order_id": result.order.client_order_id,
                    "status": result.order.lifecycle.status.value,
                    "filled_quantity": str(result.order.lifecycle.filled_quantity),
                }
            )
        return {"writes_attempted": writes, "outcomes": outcomes}
    @staticmethod
    def _project(
        account: R5AccountSnapshot,
        intent: OrderIntent,
        notional: Decimal,
    ) -> R5AccountSnapshot:
        long_value = account.long_notional
        short_value = account.short_notional
        add = not intent.action.reduces_risk
        if intent.position_side.value == "LONG":
            long_value = long_value + notional if add else max(Decimal("0"), long_value - notional)
        else:
            short_value = short_value + notional if add else max(Decimal("0"), short_value - notional)
        tif = str(intent.metadata.get("time_in_force", "")).upper()
        open_orders = account.open_orders + (1 if tif in {"GTC", "GTX"} else 0)
        return R5AccountSnapshot(
            account.account_id,
            account.equity,
            long_value,
            short_value,
            open_orders,
            account.unmanaged_positions,
            datetime.now(UTC),
        )

