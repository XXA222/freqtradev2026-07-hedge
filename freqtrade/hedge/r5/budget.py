from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator, Protocol, runtime_checkable
from uuid import uuid4

from .models import R5AccountSnapshot, R5LimitProfile, exact_decimal


@dataclass(frozen=True, slots=True)
class R5BudgetSnapshot:
    account_id: str
    utc_date: str
    orders: int
    turnover: Decimal
    realized_loss: Decimal
    gross_peak: Decimal
    net_peak: Decimal
    open_orders_peak: int


class R5BudgetExceeded(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class R5BudgetReservation:
    reservation_id: str
    account_id: str
    utc_date: str
    orders: int
    turnover: Decimal
    state: str


@runtime_checkable
class R5BudgetPort(Protocol):
    """Authoritative budget contract shared by legacy and SQL implementations."""

    def prepare_orders(
        self,
        *,
        account_id: str,
        profile: R5LimitProfile,
        reservations: Iterable[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ],
    ) -> R5BudgetReservation | None: ...

    def confirm_reservation(self, reservation_id: str) -> R5BudgetSnapshot: ...

    def release_reservation(self, reservation_id: str) -> R5BudgetSnapshot: ...

    def reservation(self, reservation_id: str) -> R5BudgetReservation | None: ...

    def record_realized_pnl(
        self,
        *,
        account_id: str,
        realized_pnl: Decimal,
        profile: R5LimitProfile,
    ) -> R5BudgetSnapshot: ...

    def snapshot(self, account_id: str) -> R5BudgetSnapshot: ...


class R5DailyBudgetStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._initialize()

    def __enter__(self) -> "R5DailyBudgetStore":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.path.exists():
            with self._connect(allow_closed=True) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._closed = True

    def reserve_order(
        self,
        *,
        account_id: str,
        profile: R5LimitProfile,
        order_notional: Decimal,
        projected: R5AccountSnapshot,
        current: R5AccountSnapshot | None = None,
        reduces_risk: bool = False,
    ) -> R5BudgetSnapshot:
        return self.reserve_orders(
            account_id=account_id,
            profile=profile,
            reservations=((order_notional, projected, current, reduces_risk),),
        )

    def reserve_orders(
        self,
        *,
        account_id: str,
        profile: R5LimitProfile,
        reservations: Iterable[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ],
    ) -> R5BudgetSnapshot:
        """Compatibility API: atomically reserve and immediately confirm the batch."""

        reservation = self.prepare_orders(
            account_id=account_id,
            profile=profile,
            reservations=reservations,
        )
        if reservation is None:
            return self.snapshot(account_id)
        return self.confirm_reservation(reservation.reservation_id)

    def prepare_orders(
        self,
        *,
        account_id: str,
        profile: R5LimitProfile,
        reservations: Iterable[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ],
    ) -> R5BudgetReservation | None:
        """Reserve a complete batch before the first write using PREPARED state.

        A crash leaves a conservative PREPARED reservation that can be confirmed after
        order recovery or explicitly released only when the caller proves no side effect
        occurred. This replaces irreversible pre-submit turnover accounting.
        """

        items = tuple(reservations)
        if not items:
            return None
        total_turnover = Decimal("0")
        for order_notional, projected, current, reduces_risk in items:
            notional = exact_decimal(order_notional, "order_notional")
            if notional <= 0 or notional > profile.max_single_order_notional:
                raise R5BudgetExceeded("R5 single-order notional limit exceeded")
            if projected.account_id != account_id:
                raise R5BudgetExceeded("R5 budget account mismatch")
            if projected.unmanaged_positions:
                raise R5BudgetExceeded("R5 unmanaged position blocks all new exchange writes")
            if projected.open_orders > profile.max_open_orders:
                raise R5BudgetExceeded("R5 projected open-order limit exceeded")
            over_limits = (
                projected.gross_notional > profile.max_gross_notional
                or projected.net_notional > profile.max_net_notional
            )
            if over_limits:
                allowed_deleveraging = (
                    bool(reduces_risk)
                    and current is not None
                    and projected.gross_notional <= current.gross_notional
                    and projected.net_notional <= current.net_notional
                )
                if not allowed_deleveraging:
                    raise R5BudgetExceeded("R5 projected exposure limit exceeded")
            total_turnover += notional

        day = datetime.now(UTC).date().isoformat()
        reservation_id = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM r5_daily_budget WHERE account_id=? AND utc_date=?",
                (account_id, day),
            ).fetchone()
            current_budget = self._snapshot(row, account_id, day)
            next_orders = current_budget.orders + len(items)
            next_turnover = current_budget.turnover + total_turnover
            if next_orders > profile.max_daily_orders:
                raise R5BudgetExceeded("R5 daily order-count limit exceeded")
            if next_turnover > profile.max_daily_turnover:
                raise R5BudgetExceeded("R5 daily turnover limit exceeded")
            if current_budget.realized_loss > profile.max_daily_loss:
                raise R5BudgetExceeded("R5 daily loss limit exceeded")
            gross_peak = max(current_budget.gross_peak, *(item[1].gross_notional for item in items))
            net_peak = max(current_budget.net_peak, *(item[1].net_notional for item in items))
            open_peak = max(current_budget.open_orders_peak, *(item[1].open_orders for item in items))
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """INSERT INTO r5_daily_budget(
                    account_id,utc_date,orders,turnover,realized_loss,gross_peak,net_peak,open_orders_peak,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,utc_date) DO UPDATE SET
                    orders=excluded.orders,turnover=excluded.turnover,gross_peak=excluded.gross_peak,
                    net_peak=excluded.net_peak,open_orders_peak=excluded.open_orders_peak,updated_at=excluded.updated_at""",
                (account_id, day, next_orders, str(next_turnover), str(current_budget.realized_loss),
                 str(gross_peak), str(net_peak), open_peak, now),
            )
            connection.execute(
                """INSERT INTO r5_budget_reservations(
                    reservation_id,account_id,utc_date,orders,turnover,state,created_at,updated_at
                ) VALUES(?,?,?,?,?,'PREPARED',?,?)""",
                (reservation_id, account_id, day, len(items), str(total_turnover), now, now),
            )
            connection.commit()
        return R5BudgetReservation(reservation_id, account_id, day, len(items), total_turnover, "PREPARED")

    def confirm_reservation(self, reservation_id: str) -> R5BudgetSnapshot:
        value = str(reservation_id).strip()
        if not value:
            raise ValueError("reservation_id is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM r5_budget_reservations WHERE reservation_id=?", (value,)
            ).fetchone()
            if row is None:
                raise KeyError(value)
            state = str(row["state"])
            if state == "RELEASED":
                raise RuntimeError("R5 budget reservation was released")
            if state == "PREPARED":
                connection.execute(
                    "UPDATE r5_budget_reservations SET state='CONFIRMED',updated_at=? WHERE reservation_id=?",
                    (datetime.now(UTC).isoformat(), value),
                )
            account_id = str(row["account_id"])
            day = str(row["utc_date"])
            budget_row = connection.execute(
                "SELECT * FROM r5_daily_budget WHERE account_id=? AND utc_date=?",
                (account_id, day),
            ).fetchone()
            connection.commit()
        return self._snapshot(budget_row, account_id, day)

    def release_reservation(self, reservation_id: str) -> R5BudgetSnapshot:
        value = str(reservation_id).strip()
        if not value:
            raise ValueError("reservation_id is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM r5_budget_reservations WHERE reservation_id=?", (value,)
            ).fetchone()
            if row is None:
                raise KeyError(value)
            state = str(row["state"])
            account_id = str(row["account_id"])
            day = str(row["utc_date"])
            if state == "CONFIRMED":
                raise RuntimeError("R5 confirmed budget reservation cannot be released")
            if state == "PREPARED":
                budget_row = connection.execute(
                    "SELECT * FROM r5_daily_budget WHERE account_id=? AND utc_date=?",
                    (account_id, day),
                ).fetchone()
                current = self._snapshot(budget_row, account_id, day)
                next_orders = max(0, current.orders - int(row["orders"]))
                next_turnover = max(Decimal("0"), current.turnover - Decimal(str(row["turnover"])))
                connection.execute(
                    "UPDATE r5_daily_budget SET orders=?,turnover=?,updated_at=? WHERE account_id=? AND utc_date=?",
                    (next_orders, str(next_turnover), datetime.now(UTC).isoformat(), account_id, day),
                )
                connection.execute(
                    "UPDATE r5_budget_reservations SET state='RELEASED',updated_at=? WHERE reservation_id=?",
                    (datetime.now(UTC).isoformat(), value),
                )
            final_row = connection.execute(
                "SELECT * FROM r5_daily_budget WHERE account_id=? AND utc_date=?",
                (account_id, day),
            ).fetchone()
            connection.commit()
        return self._snapshot(final_row, account_id, day)

    def reservation(self, reservation_id: str) -> R5BudgetReservation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM r5_budget_reservations WHERE reservation_id=?",
                (str(reservation_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        return R5BudgetReservation(
            str(row["reservation_id"]), str(row["account_id"]), str(row["utc_date"]),
            int(row["orders"]), Decimal(str(row["turnover"])), str(row["state"]),
        )

    def record_realized_pnl(self, *, account_id: str, realized_pnl: Decimal, profile: R5LimitProfile) -> R5BudgetSnapshot:
        pnl = exact_decimal(realized_pnl, "realized_pnl")
        day = datetime.now(UTC).date().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM r5_daily_budget WHERE account_id=? AND utc_date=?", (account_id, day)).fetchone()
            current = self._snapshot(row, account_id, day)
            loss = current.realized_loss + max(Decimal("0"), -pnl)
            connection.execute(
                """INSERT INTO r5_daily_budget(account_id,utc_date,orders,turnover,realized_loss,gross_peak,net_peak,open_orders_peak,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id,utc_date) DO UPDATE SET realized_loss=excluded.realized_loss,updated_at=excluded.updated_at""",
                (account_id, day, current.orders, str(current.turnover), str(loss), str(current.gross_peak), str(current.net_peak), current.open_orders_peak, datetime.now(UTC).isoformat()),
            )
            connection.commit()
        result = R5BudgetSnapshot(account_id, day, current.orders, current.turnover, loss, current.gross_peak, current.net_peak, current.open_orders_peak)
        if loss > profile.max_daily_loss:
            raise R5BudgetExceeded("R5 daily loss limit exceeded")
        return result

    def snapshot(self, account_id: str) -> R5BudgetSnapshot:
        day = datetime.now(UTC).date().isoformat()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM r5_daily_budget WHERE account_id=? AND utc_date=?", (account_id, day)).fetchone()
        return self._snapshot(row, account_id, day)

    @contextmanager
    def _connect(self, *, allow_closed: bool = False) -> Iterator[sqlite3.Connection]:
        if not allow_closed:
            self._ensure_open()
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS r5_daily_budget(
                    account_id TEXT NOT NULL,utc_date TEXT NOT NULL,orders INTEGER NOT NULL,turnover TEXT NOT NULL,
                    realized_loss TEXT NOT NULL,gross_peak TEXT NOT NULL,net_peak TEXT NOT NULL,
                    open_orders_peak INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(account_id,utc_date))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS r5_budget_reservations(
                    reservation_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,utc_date TEXT NOT NULL,
                    orders INTEGER NOT NULL,turnover TEXT NOT NULL,state TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    CHECK(state IN ('PREPARED','CONFIRMED','RELEASED')))"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_r5_budget_reservation_scope "
                "ON r5_budget_reservations(account_id,utc_date,state)"
            )

    @staticmethod
    def _snapshot(row: sqlite3.Row | None, account_id: str, day: str) -> R5BudgetSnapshot:
        if row is None:
            return R5BudgetSnapshot(account_id, day, 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 0)
        return R5BudgetSnapshot(
            account_id, day, int(row["orders"]), Decimal(row["turnover"]), Decimal(row["realized_loss"]),
            Decimal(row["gross_peak"]), Decimal(row["net_peak"]), int(row["open_orders_peak"]),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("R5 daily budget store is closed")
