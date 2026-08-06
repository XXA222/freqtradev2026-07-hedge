from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

from .budget import R5DailyBudgetStore
from .models import R5AccountSnapshot, R5LimitProfile


@dataclass(frozen=True, slots=True)
class R5AutoSessionResult:
    cycles_attempted: int
    cycles_completed: int
    writes_attempted: int
    started_at: datetime
    completed_at: datetime
    final_snapshot: R5AccountSnapshot
    stopped_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "cycles_attempted": self.cycles_attempted,
            "cycles_completed": self.cycles_completed,
            "writes_attempted": self.writes_attempted,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "stopped_reason": self.stopped_reason,
            "final_gross_notional": str(self.final_snapshot.gross_notional),
            "final_net_notional": str(self.final_snapshot.net_notional),
            "final_open_orders": self.final_snapshot.open_orders,
        }


class R5AutoSession:
    """Bounded host for restricted automated execution.

    Trading logic is injected. This class owns only safety boundaries, duration, budgets,
    and fail-closed account checks. It never silently promotes limits.
    """

    def __init__(
        self,
        *,
        profile: R5LimitProfile,
        budget: R5DailyBudgetStore,
        account_id: str,
        snapshot_provider: Callable[[], R5AccountSnapshot],
        cycle: Callable[[int, R5AccountSnapshot], Mapping[str, object]],
        stop_requested: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile
        self.budget = budget
        self.account_id = account_id
        self.snapshot_provider = snapshot_provider
        self.cycle = cycle
        self.stop_requested = stop_requested or (lambda: False)
        self.monotonic = monotonic

    def run(self, *, max_cycles: int = 60) -> R5AutoSessionResult:
        if not isinstance(max_cycles, int) or isinstance(max_cycles, bool) or not 1 <= max_cycles <= 10000:
            raise ValueError("max_cycles must be in [1, 10000]")
        started_at = datetime.now(UTC)
        deadline = self.monotonic() + self.profile.max_runtime_minutes * 60
        attempts = 0
        completed = 0
        writes = 0
        reason = "MAX_CYCLES"
        snapshot = self.snapshot_provider()
        self._check(snapshot)
        for index in range(max_cycles):
            if self.stop_requested():
                reason = "STOP_REQUESTED"
                break
            if self.monotonic() >= deadline:
                reason = "RUNTIME_LIMIT"
                break
            snapshot = self.snapshot_provider()
            self._check(snapshot)
            attempts += 1
            result = self.cycle(index, snapshot)
            cycle_writes = int(result.get("writes_attempted", 0))
            if cycle_writes < 0:
                raise RuntimeError("R5 automatic cycle returned invalid write count")
            writes += cycle_writes
            if "realized_pnl" in result:
                self.budget.record_realized_pnl(
                    account_id=self.account_id,
                    realized_pnl=result["realized_pnl"],
                    profile=self.profile,
                )
            if writes > self.profile.write_request_budget:
                raise PermissionError("R5 automatic session exceeded write-request budget")
            snapshot = self.snapshot_provider()
            self._check(snapshot)
            completed += 1
        else:
            reason = "MAX_CYCLES"
        return R5AutoSessionResult(
            attempts,
            completed,
            writes,
            started_at,
            datetime.now(UTC),
            snapshot,
            reason,
        )

    def _check(self, snapshot: R5AccountSnapshot) -> None:
        if snapshot.account_id != self.account_id:
            raise PermissionError("R5 automatic account mismatch")
        if snapshot.unmanaged_positions:
            raise PermissionError("R5 automatic execution blocked by unmanaged positions")
        if snapshot.gross_notional > self.profile.max_gross_notional:
            raise PermissionError("R5 automatic gross-notional limit exceeded")
        if snapshot.net_notional > self.profile.max_net_notional:
            raise PermissionError("R5 automatic net-notional limit exceeded")
        if snapshot.open_orders > self.profile.max_open_orders:
            raise PermissionError("R5 automatic open-order limit exceeded")
        daily = self.budget.snapshot(self.account_id)
        if daily.realized_loss > self.profile.max_daily_loss:
            raise PermissionError("R5 automatic daily loss limit exceeded")
