from __future__ import annotations

from datetime import UTC, datetime

from freqtrade.hedge.r5.models import R5Stage

from .models import GateDecision
from .promotion import R54PromotionEngine
from .store import R54OperationalStore


class R54ReadinessEvaluator:
    def __init__(self, store: R54OperationalStore) -> None:
        self.store = store
        self.promotions = R54PromotionEngine(store)

    def evaluate_all(
        self,
        *,
        account_id: str,
        now: datetime | None = None,
    ) -> tuple[GateDecision, ...]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        decisions: list[GateDecision] = []
        for stage in R5Stage:
            decision = self.promotions.evaluate(account_id=account_id, stage=stage, now=current)
            decisions.append(decision)
        return tuple(decisions)

    def highest_ready_stage(self, *, account_id: str) -> R5Stage | None:
        ready: R5Stage | None = None
        for decision in self.evaluate_all(account_id=account_id):
            if not decision.ready:
                break
            ready = decision.stage
        return ready
