"""Bounded 1-minute production session with durable candle cursor."""

from __future__ import annotations

import time
from uuid import uuid4
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Callable, Mapping

from .arming import R5StageArmAuthority, R5StagePermit
from .budget import R5DailyBudgetStore
from .models import R5AccountSnapshot, R5LimitProfile
from .strategy_store import R5StrategyStateStore


@dataclass(frozen=True, slots=True)
class R5ClosedCandle:
    close_time: datetime
    decision_id: str

    def __post_init__(self) -> None:
        if self.close_time.tzinfo is None or self.close_time.utcoffset() is None:
            raise ValueError("R5 candle close must be timezone-aware")
        if not str(self.decision_id).strip():
            raise ValueError("R5 decision_id is required")


@dataclass(frozen=True, slots=True)
class R5ProductionSessionReport:
    cycles_completed: int
    duplicate_candles_skipped: int
    writes_attempted: int
    stopped_reason: str
    started_at: datetime
    completed_at: datetime


class R5ClosedCandleProductionSession:
    """Execute at most once per durable closed 1m candle.

    The session refreshes clock evidence periodically, records realized PnL from an
    authoritative ledger provider, and renews a short-lived permit through the issuing
    authority. Revocation remains immediate because every permit use rechecks authority.
    """

    def __init__(
        self,
        *,
        account_id: str,
        symbol: str,
        profile: R5LimitProfile,
        store: R5StrategyStateStore,
        budget: R5DailyBudgetStore,
        permit: R5StagePermit,
        authority: R5StageArmAuthority,
        snapshot_provider: Callable[[], R5AccountSnapshot],
        closed_candle_provider: Callable[[], R5ClosedCandle | None],
        cycle: Callable[[R5ClosedCandle, R5AccountSnapshot], Mapping[str, object]],
        realized_pnl_provider: Callable[[], Decimal],
        clock_refresh: Callable[[], None],
        stop_requested: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = 1.0,
        clock_refresh_seconds: float = 30.0,
        permit_renew_seconds: float = 120.0,
    ) -> None:
        if symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("R5 production symbol is not allowlisted")
        if poll_seconds <= 0 or poll_seconds > 60:
            raise ValueError("poll_seconds must be in (0, 60]")
        self.account_id = account_id
        self.symbol = symbol
        self.profile = profile
        self.store = store
        self.budget = budget
        self.permit = permit
        self.authority = authority
        self.snapshot_provider = snapshot_provider
        self.closed_candle_provider = closed_candle_provider
        self.cycle = cycle
        self.realized_pnl_provider = realized_pnl_provider
        self.clock_refresh = clock_refresh
        self.stop_requested = stop_requested or (lambda: False)
        self.sleep = sleep
        self.monotonic = monotonic
        self.poll_seconds = poll_seconds
        self.clock_refresh_seconds = clock_refresh_seconds
        self.permit_renew_seconds = permit_renew_seconds

    def run(self, *, max_cycles: int = 60) -> R5ProductionSessionReport:
        if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or not 1 <= max_cycles <= 10000:
            raise ValueError("max_cycles must be in [1, 10000]")
        started = datetime.now(UTC)
        deadline = self.monotonic() + self.profile.max_runtime_minutes * 60
        last_clock_refresh = float("-inf")
        last_permit_renew = self.monotonic()
        # Binance Income is the sole financial daily-loss authority.  The provider
        # remains available for observability/consistency checks but never mutates
        # the SQL loss budget from planner inventory PnL.
        self.realized_pnl_provider()
        cycles = 0
        skipped = 0
        writes = 0
        lease_owner = f"r5-session:{uuid4().hex}"
        reason = "MAX_CYCLES"
        while cycles < max_cycles:
            now_mono = self.monotonic()
            if self.stop_requested():
                reason = "STOP_REQUESTED"
                break
            if now_mono >= deadline:
                reason = "RUNTIME_LIMIT"
                break
            if now_mono - last_clock_refresh >= self.clock_refresh_seconds:
                self.clock_refresh()
                last_clock_refresh = now_mono
            if now_mono - last_permit_renew >= self.permit_renew_seconds:
                self.authority.renew(self.permit, ttl_seconds=300)
                last_permit_renew = now_mono
            candle = self.closed_candle_provider()
            if candle is None:
                self.sleep(self.poll_seconds)
                continue
            claimed = self.store.begin_decision_cursor(
                account_id=self.account_id,
                symbol=self.symbol,
                timeframe="1m",
                candle_close=candle.close_time,
                decision_id=candle.decision_id,
                lease_owner=lease_owner,
            )
            if not claimed:
                skipped += 1
                self.sleep(self.poll_seconds)
                continue
            try:
                snapshot = self.snapshot_provider()
                self._check_snapshot(snapshot)
                result = self.cycle(candle, snapshot)
                cycle_writes = int(result.get("writes_attempted", 0))
                if cycle_writes < 0:
                    raise RuntimeError("R5 production cycle returned invalid write count")
                projected_writes = writes + cycle_writes
                if projected_writes > self.profile.write_request_budget:
                    raise PermissionError("R5 production session exceeded write budget")
                self.realized_pnl_provider()
                self._check_snapshot(self.snapshot_provider())
                self.store.complete_decision_cursor(
                    account_id=self.account_id,
                    symbol=self.symbol,
                    timeframe="1m",
                    decision_id=candle.decision_id,
                    lease_owner=lease_owner,
                )
                writes = projected_writes
                cycles += 1
            except Exception:
                self.store.release_decision_cursor(
                    account_id=self.account_id,
                    symbol=self.symbol,
                    timeframe="1m",
                    decision_id=candle.decision_id,
                    lease_owner=lease_owner,
                )
                raise
        return R5ProductionSessionReport(
            cycles,
            skipped,
            writes,
            reason,
            started,
            datetime.now(UTC),
        )

    def _check_snapshot(self, snapshot: R5AccountSnapshot) -> None:
        if snapshot.account_id != self.account_id:
            raise PermissionError("R5 production account mismatch")
        if snapshot.unmanaged_positions:
            raise PermissionError("R5 production blocked by unmanaged positions")
        if snapshot.open_orders > self.profile.max_open_orders:
            raise PermissionError("R5 production open-order limit exceeded")
        # A pre-existing over-limit account may only be handled by the batch executor's
        # strict risk-reducing exception; the session itself never authorizes new risk.
        daily = self.budget.snapshot(self.account_id)
        if daily.realized_loss > self.profile.max_daily_loss:
            raise PermissionError("R5 production daily loss limit exceeded")
