"""SQL-authoritative execution budget and Binance income facts.

The production execution graph uses the same SQL database for order lifecycle,
immutable fills, audit/outbox evidence, and budget reservations.  The legacy raw
SQLite budget store remains available only for backward-compatible replay tools.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable, Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from freqtrade.persistence.hedge_models import (
    R5BudgetReservationRow,
    R5DailyBudgetRow,
    R5IncomeEventRow,
    canonical_decimal,
)

from .budget import (
    R5BudgetExceeded,
    R5BudgetReservation,
    R5BudgetSnapshot,
)
from .models import R5AccountSnapshot, R5LimitProfile, exact_decimal


def _day(value: datetime | None = None) -> str:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("budget timestamp must be timezone-aware")
    return moment.astimezone(UTC).date().isoformat()


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


class SqlR5DailyBudgetStore:
    """Restart-safe, multi-process daily budget in the authoritative SQL ledger."""

    def __init__(self, session_factory: object) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        self._session_factory = session_factory

    @staticmethod
    def _snapshot(row: R5DailyBudgetRow | None, account_id: str, day: str) -> R5BudgetSnapshot:
        if row is None:
            return R5BudgetSnapshot(
                account_id,
                day,
                0,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                0,
            )
        return R5BudgetSnapshot(
            account_id=str(row.account_id),
            utc_date=str(row.utc_date),
            orders=int(row.orders),
            turnover=Decimal(str(row.turnover)),
            realized_loss=Decimal(str(row.realized_loss)),
            gross_peak=Decimal(str(row.gross_peak)),
            net_peak=Decimal(str(row.net_peak)),
            open_orders_peak=int(row.open_orders_peak),
        )

    @staticmethod
    def _validate_batch(
        *,
        account_id: str,
        profile: R5LimitProfile,
        reservations: tuple[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool], ...
        ],
    ) -> Decimal:
        total = Decimal("0")
        for order_notional, projected, current, reduces_risk in reservations:
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
            total += notional
        return total

    @staticmethod
    def _locked_budget(session: object, *, account_id: str, day: str) -> R5DailyBudgetRow:
        row = session.scalar(  # type: ignore[attr-defined]
            select(R5DailyBudgetRow)
            .where(
                R5DailyBudgetRow.account_id == account_id,
                R5DailyBudgetRow.utc_date == day,
            )
            .with_for_update()
        )
        if row is not None:
            # A harmless revision update makes SQLite acquire its write lock before
            # calculating the next budget values. PostgreSQL already has FOR UPDATE.
            row.revision = int(row.revision) + 1
            session.flush()  # type: ignore[attr-defined]
            return row
        row = R5DailyBudgetRow(
            account_id=account_id,
            utc_date=day,
            orders=0,
            turnover="0",
            realized_loss="0",
            gross_peak="0",
            net_peak="0",
            open_orders_peak=0,
            revision=1,
        )
        session.add(row)  # type: ignore[attr-defined]
        session.flush()  # type: ignore[attr-defined]
        return row

    def prepare_orders(
        self,
        *,
        account_id: str,
        profile: R5LimitProfile,
        reservations: Iterable[
            tuple[Decimal, R5AccountSnapshot, R5AccountSnapshot | None, bool]
        ],
    ) -> R5BudgetReservation | None:
        items = tuple(reservations)
        if not items:
            return None
        total = self._validate_batch(
            account_id=account_id,
            profile=profile,
            reservations=items,
        )
        day = _day()
        reservation_id = uuid4().hex
        try:
            with self._session_factory.begin() as session:  # type: ignore[operator]
                row = self._locked_budget(session, account_id=account_id, day=day)
                current = self._snapshot(row, account_id, day)
                next_orders = current.orders + len(items)
                next_turnover = current.turnover + total
                if next_orders > profile.max_daily_orders:
                    raise R5BudgetExceeded("R5 daily order-count limit exceeded")
                if next_turnover > profile.max_daily_turnover:
                    raise R5BudgetExceeded("R5 daily turnover limit exceeded")
                if current.realized_loss > profile.max_daily_loss:
                    raise R5BudgetExceeded("R5 daily loss limit exceeded")
                row.orders = next_orders
                row.turnover = canonical_decimal(next_turnover)
                row.gross_peak = canonical_decimal(
                    max(current.gross_peak, *(item[1].gross_notional for item in items))
                )
                row.net_peak = canonical_decimal(
                    max(current.net_peak, *(item[1].net_notional for item in items))
                )
                row.open_orders_peak = max(
                    current.open_orders_peak,
                    *(item[1].open_orders for item in items),
                )
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(
                    R5BudgetReservationRow(
                        reservation_id=reservation_id,
                        account_id=account_id,
                        utc_date=day,
                        orders=len(items),
                        turnover=canonical_decimal(total),
                        state="PREPARED",
                        metadata_json=json.dumps(
                            {"profile_stage": profile.stage.value},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                )
        except IntegrityError as exc:
            raise RuntimeError("R5 budget concurrency conflict; retry the complete batch") from exc
        return R5BudgetReservation(
            reservation_id,
            account_id,
            day,
            len(items),
            total,
            "PREPARED",
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
        prepared = self.prepare_orders(
            account_id=account_id,
            profile=profile,
            reservations=reservations,
        )
        return self.snapshot(account_id) if prepared is None else self.confirm_reservation(prepared.reservation_id)

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

    def reservation(self, reservation_id: str) -> R5BudgetReservation | None:
        value = str(reservation_id).strip()
        if not value:
            raise ValueError("reservation_id is required")
        with self._session_factory() as session:  # type: ignore[operator]
            row = session.get(R5BudgetReservationRow, value)
            if row is None:
                return None
            return R5BudgetReservation(
                row.reservation_id,
                row.account_id,
                row.utc_date,
                int(row.orders),
                Decimal(str(row.turnover)),
                row.state,
            )

    def confirm_reservation(self, reservation_id: str) -> R5BudgetSnapshot:
        value = str(reservation_id).strip()
        if not value:
            raise ValueError("reservation_id is required")
        with self._session_factory.begin() as session:  # type: ignore[operator]
            row = session.scalar(
                select(R5BudgetReservationRow)
                .where(R5BudgetReservationRow.reservation_id == value)
                .with_for_update()
            )
            if row is None:
                raise KeyError(value)
            if row.state == "RELEASED":
                raise RuntimeError("R5 budget reservation was released")
            if row.state == "PREPARED":
                row.state = "CONFIRMED"
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            account_id, day = row.account_id, row.utc_date
        return self.snapshot(account_id, utc_date=day)

    def release_reservation(self, reservation_id: str) -> R5BudgetSnapshot:
        value = str(reservation_id).strip()
        if not value:
            raise ValueError("reservation_id is required")
        with self._session_factory.begin() as session:  # type: ignore[operator]
            reservation = session.scalar(
                select(R5BudgetReservationRow)
                .where(R5BudgetReservationRow.reservation_id == value)
                .with_for_update()
            )
            if reservation is None:
                raise KeyError(value)
            if reservation.state == "CONFIRMED":
                raise RuntimeError("R5 confirmed budget reservation cannot be released")
            budget = self._locked_budget(
                session,
                account_id=reservation.account_id,
                day=reservation.utc_date,
            )
            if reservation.state == "PREPARED":
                budget.orders = max(0, int(budget.orders) - int(reservation.orders))
                budget.turnover = canonical_decimal(
                    max(
                        Decimal("0"),
                        Decimal(str(budget.turnover)) - Decimal(str(reservation.turnover)),
                    )
                )
                budget.updated_at = datetime.now(UTC).replace(tzinfo=None)
                reservation.state = "RELEASED"
                reservation.updated_at = datetime.now(UTC).replace(tzinfo=None)
            account_id, day = reservation.account_id, reservation.utc_date
        return self.snapshot(account_id, utc_date=day)

    def record_realized_pnl(
        self,
        *,
        account_id: str,
        realized_pnl: Decimal,
        profile: R5LimitProfile,
        occurred_at: datetime | None = None,
    ) -> R5BudgetSnapshot:
        pnl = exact_decimal(realized_pnl, "realized_pnl")
        day = _day(occurred_at)
        with self._session_factory.begin() as session:  # type: ignore[operator]
            row = self._locked_budget(session, account_id=account_id, day=day)
            loss = Decimal(str(row.realized_loss)) + max(Decimal("0"), -pnl)
            row.realized_loss = canonical_decimal(loss)
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        result = self.snapshot(account_id, utc_date=day)
        if result.realized_loss > profile.max_daily_loss:
            raise R5BudgetExceeded("R5 daily loss limit exceeded")
        return result

    def record_income_event(
        self,
        *,
        account_id: str,
        external_event_id: str,
        income_type: str,
        amount: Decimal,
        occurred_at: datetime,
        profile: R5LimitProfile,
        symbol: str | None = None,
        asset: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[bool, R5BudgetSnapshot]:
        """Persist an immutable exchange income fact and update loss exactly once."""

        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("income timestamp must be timezone-aware")
        event_id = str(external_event_id).strip()
        kind = str(income_type).strip().upper()
        if not event_id or not kind:
            raise ValueError("income event identity is incomplete")
        value = exact_decimal(amount, "income_amount")
        day = _day(occurred_at)
        inserted = False
        try:
            with self._session_factory.begin() as session:  # type: ignore[operator]
                existing = session.scalar(
                    select(R5IncomeEventRow).where(
                        R5IncomeEventRow.exchange == "binance",
                        R5IncomeEventRow.account_id == account_id,
                        R5IncomeEventRow.external_event_id == event_id,
                    )
                )
                if existing is not None:
                    if (
                        existing.income_type != kind
                        or Decimal(str(existing.amount)) != value
                        or existing.symbol != symbol
                    ):
                        raise ValueError("income event id reused with conflicting content")
                else:
                    session.add(
                        R5IncomeEventRow(
                            exchange="binance",
                            account_id=account_id,
                            external_event_id=event_id,
                            income_type=kind,
                            symbol=symbol,
                            asset=asset,
                            amount=canonical_decimal(value),
                            occurred_at=_naive_utc(occurred_at),
                            payload_json=json.dumps(
                                dict(payload or {}),
                                sort_keys=True,
                                separators=(",", ":"),
                                default=str,
                            ),
                        )
                    )
                    budget = self._locked_budget(session, account_id=account_id, day=day)
                    budget.realized_loss = canonical_decimal(
                        Decimal(str(budget.realized_loss)) + max(Decimal("0"), -value)
                    )
                    budget.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    inserted = True
        except IntegrityError:
            # A concurrent process won the immutable fact insert. Re-read and validate.
            return self.record_income_event(
                account_id=account_id,
                external_event_id=event_id,
                income_type=kind,
                amount=value,
                occurred_at=occurred_at,
                profile=profile,
                symbol=symbol,
                asset=asset,
                payload=payload,
            )
        snapshot = self.snapshot(account_id, utc_date=day)
        if snapshot.realized_loss > profile.max_daily_loss:
            raise R5BudgetExceeded("R5 daily loss limit exceeded")
        return inserted, snapshot

    def snapshot(self, account_id: str, *, utc_date: str | None = None) -> R5BudgetSnapshot:
        day = utc_date or _day()
        with self._session_factory() as session:  # type: ignore[operator]
            row = session.scalar(
                select(R5DailyBudgetRow).where(
                    R5DailyBudgetRow.account_id == account_id,
                    R5DailyBudgetRow.utc_date == day,
                )
            )
            return self._snapshot(row, account_id, day)

    def authoritative_realized_loss(self, account_id: str) -> Decimal:
        return self.snapshot(account_id).realized_loss
