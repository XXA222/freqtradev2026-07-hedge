from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from freqtrade.hedge.planning.context import (
    LegPosition,
    PositionSide,
    StrategyLegState,
    TacticalLot,
    TrailingPhase,
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _state_to_dict(state: StrategyLegState) -> dict[str, Any]:
    raw = asdict(state)
    raw["side"] = state.side.value
    raw["trailing_phase"] = state.trailing_phase.value
    for name in (
        "last_entry_at",
        "last_reduce_at",
        "trailing_started_at",
        "trailing_confirmed_at",
        "trailing_cooldown_until",
        "last_unstuck_at",
    ):
        value = getattr(state, name)
        raw[name] = None if value is None else value.isoformat()
    for name in (
        "trailing_extreme",
        "trailing_trigger_price",
        "unstuck_daily_loss",
        "unstuck_weekly_loss",
    ):
        value = getattr(state, name)
        raw[name] = None if value is None else str(value)
    return raw


def _state_from_dict(value: Mapping[str, Any]) -> StrategyLegState:
    kwargs = dict(value)
    kwargs["side"] = PositionSide(str(kwargs["side"]))
    kwargs["trailing_phase"] = TrailingPhase(str(kwargs.get("trailing_phase", "IDLE")))
    for name in (
        "last_entry_at",
        "last_reduce_at",
        "trailing_started_at",
        "trailing_confirmed_at",
        "trailing_cooldown_until",
        "last_unstuck_at",
    ):
        item = kwargs.get(name)
        kwargs[name] = None if item in (None, "") else datetime.fromisoformat(str(item))
    for name in (
        "trailing_extreme",
        "trailing_trigger_price",
        "unstuck_daily_loss",
        "unstuck_weekly_loss",
    ):
        item = kwargs.get(name)
        kwargs[name] = None if item in (None, "") else Decimal(str(item))
    return StrategyLegState(**kwargs)


def _leg_to_dict(leg: LegPosition) -> dict[str, Any]:
    return {
        "side": leg.side.value,
        "quantity": str(leg.quantity),
        "average_price": str(leg.average_price),
        "core_quantity": str(leg.core_quantity),
        "core_average_price": str(leg.core_average_price),
        "tactical_quantity": str(leg.tactical_quantity),
        "tactical_average_price": str(leg.tactical_average_price),
        "realized_pnl": str(leg.realized_pnl),
        "tactical_realized_pnl": str(leg.tactical_realized_pnl),
        "tactical_lots": [
            {
                "lot_id": lot.lot_id,
                "quantity": str(lot.quantity),
                "average_price": str(lot.average_price),
                "opened_at": lot.opened_at.isoformat(),
                "layer": lot.layer,
                "realized_pnl": str(lot.realized_pnl),
                "fees": str(lot.fees),
                "funding": str(lot.funding),
                "closed_quantity": str(lot.closed_quantity),
            }
            for lot in leg.tactical_lots
        ],
    }


def _leg_from_dict(value: Mapping[str, Any]) -> LegPosition:
    lots = tuple(
        TacticalLot(
            lot_id=str(item["lot_id"]),
            quantity=Decimal(str(item["quantity"])),
            average_price=Decimal(str(item["average_price"])),
            opened_at=datetime.fromisoformat(str(item["opened_at"])),
            layer=int(item.get("layer", 0)),
            realized_pnl=Decimal(str(item.get("realized_pnl", "0"))),
            fees=Decimal(str(item.get("fees", "0"))),
            funding=Decimal(str(item.get("funding", "0"))),
            closed_quantity=Decimal(str(item.get("closed_quantity", "0"))),
        )
        for item in value.get("tactical_lots", [])
    )
    return LegPosition(
        side=PositionSide(str(value["side"])),
        quantity=Decimal(str(value.get("quantity", "0"))),
        average_price=Decimal(str(value.get("average_price", "0"))),
        core_quantity=Decimal(str(value.get("core_quantity", "0"))),
        core_average_price=Decimal(str(value.get("core_average_price", "0"))),
        tactical_quantity=Decimal(str(value.get("tactical_quantity", "0"))),
        tactical_average_price=Decimal(str(value.get("tactical_average_price", "0"))),
        realized_pnl=Decimal(str(value.get("realized_pnl", "0"))),
        tactical_realized_pnl=Decimal(str(value.get("tactical_realized_pnl", "0"))),
        tactical_lots=lots,
    )


class R5StrategyStateStore:
    """Durable authority for production strategy cycles and planner state.

    The store is deliberately separate from the order ledger.  A cycle plan is written
    before any exchange write, and leg state becomes current only when the cycle reaches
    a terminal durable state.  Replaying the same cycle id with a different input hash is
    rejected.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._initialize()

    def close(self) -> None:
        self._closed = True

    def claim_cycle(
        self,
        *,
        cycle_id: str,
        account_id: str,
        symbol: str,
        mode: str,
        input_payload: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any] | None, bool]:
        self._validate_text(cycle_id, "cycle_id", 160)
        self._validate_text(account_id, "account_id", 160)
        self._validate_text(symbol, "symbol", 32)
        self._validate_text(mode, "mode", 32)
        canonical = _canonical(input_payload)
        digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT input_sha256,status,report_json FROM r52_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_sha256"]) != digest:
                    raise RuntimeError("R5.2 cycle id was reused with different inputs")
                connection.commit()
                report = None if row["report_json"] is None else json.loads(str(row["report_json"]))
                return report, True
            connection.execute(
                """INSERT INTO r52_cycles(
                    cycle_id,account_id,symbol,mode,input_sha256,input_json,status,
                    plan_sha256,plan_json,report_json,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)""",
                (cycle_id, account_id, symbol, mode, digest, canonical, "CLAIMED", now, now),
            )
            connection.commit()
        return None, False

    def persist_plan(self, *, cycle_id: str, plan: Mapping[str, Any]) -> str:
        canonical = _canonical(plan)
        digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,plan_sha256 FROM r52_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            if row["plan_sha256"] is not None and str(row["plan_sha256"]) != digest:
                raise RuntimeError("R5.2 durable plan changed for an existing cycle")
            if str(row["status"]) not in {"CLAIMED", "PLANNED"}:
                raise RuntimeError("R5.2 cycle cannot persist a plan in its current state")
            connection.execute(
                """UPDATE r52_cycles
                   SET status='PLANNED',plan_sha256=?,plan_json=?,updated_at=?
                   WHERE cycle_id=?""",
                (digest, canonical, now, cycle_id),
            )
            self._event(connection, cycle_id, "PLAN_PERSISTED", {"plan_sha256": digest})
            connection.commit()
        return digest

    def begin_execution(self, cycle_id: str) -> None:
        self._transition(cycle_id, {"PLANNED"}, "EXECUTING", "EXECUTION_STARTED")

    def append_event(self, *, cycle_id: str, event: str, details: Mapping[str, Any]) -> None:
        self._validate_text(event, "event", 96)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM r52_cycles WHERE cycle_id=?", (cycle_id,)).fetchone() is None:
                raise KeyError(cycle_id)
            self._event(connection, cycle_id, event, details)
            connection.commit()

    def complete_cycle(
        self,
        *,
        cycle_id: str,
        report: Mapping[str, Any],
        long_state: StrategyLegState,
        short_state: StrategyLegState,
    ) -> str:
        canonical = _canonical(report)
        digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT account_id,symbol,status,report_json FROM r52_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            status = str(row["status"])
            if status == "COMPLETED":
                existing = _canonical(json.loads(str(row["report_json"])))
                if hashlib.sha256(existing.encode("ascii")).hexdigest() != digest:
                    raise RuntimeError("R5.2 completed cycle report changed")
                connection.commit()
                return digest
            if status not in {"PLANNED", "EXECUTING", "VERIFYING"}:
                raise RuntimeError("R5.2 cycle cannot complete from its current state")
            connection.execute(
                """UPDATE r52_cycles
                   SET status='COMPLETED',report_json=?,error=NULL,updated_at=?
                   WHERE cycle_id=?""",
                (canonical, now, cycle_id),
            )
            for state in (long_state, short_state):
                connection.execute(
                    """INSERT INTO r52_leg_state(account_id,symbol,side,state_json,cycle_id,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(account_id,symbol,side) DO UPDATE SET
                           state_json=excluded.state_json,
                           cycle_id=excluded.cycle_id,
                           updated_at=excluded.updated_at""",
                    (
                        str(row["account_id"]),
                        str(row["symbol"]),
                        state.side.value,
                        _canonical(_state_to_dict(state)),
                        cycle_id,
                        now,
                    ),
                )
            self._event(connection, cycle_id, "CYCLE_COMPLETED", {"report_sha256": digest})
            connection.commit()
        return digest

    def fail_cycle(self, *, cycle_id: str, error: str, recovery_required: bool) -> None:
        target = "RECOVERY_REQUIRED" if recovery_required else "FAILED"
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM r52_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            if str(row["status"]) == "COMPLETED":
                raise RuntimeError("R5.2 completed cycle cannot fail")
            connection.execute(
                "UPDATE r52_cycles SET status=?,error=?,updated_at=? WHERE cycle_id=?",
                (target, str(error)[:4096], now, cycle_id),
            )
            self._event(connection, cycle_id, target, {"error": str(error)[:1024]})
            connection.commit()

    def load_leg_states(self, *, account_id: str, symbol: str) -> tuple[StrategyLegState, StrategyLegState]:
        states: dict[str, StrategyLegState] = {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT side,state_json FROM r52_leg_state WHERE account_id=? AND symbol=?",
                (account_id, symbol),
            ).fetchall()
        for row in rows:
            states[str(row["side"])] = _state_from_dict(json.loads(str(row["state_json"])))
        return (
            states.get("LONG", StrategyLegState(PositionSide.LONG)),
            states.get("SHORT", StrategyLegState(PositionSide.SHORT)),
        )

    def adopt_core_baseline(
        self,
        *,
        account_id: str,
        symbol: str,
        long_quantity: Decimal,
        long_average_price: Decimal,
        short_quantity: Decimal,
        short_average_price: Decimal,
        manifest_sha256: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for side, quantity, price in (
                ("LONG", long_quantity, long_average_price),
                ("SHORT", short_quantity, short_average_price),
            ):
                if quantity < 0 or price < 0 or (quantity > 0 and price <= 0):
                    raise ValueError("R5.2 core baseline values are invalid")
                connection.execute(
                    """INSERT INTO r52_position_baseline(
                        account_id,symbol,side,core_quantity,core_average_price,manifest_sha256,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,symbol,side) DO UPDATE SET
                        core_quantity=excluded.core_quantity,
                        core_average_price=excluded.core_average_price,
                        manifest_sha256=excluded.manifest_sha256,
                        updated_at=excluded.updated_at""",
                    (account_id, symbol, side, str(quantity), str(price), manifest_sha256, now),
                )
                adopted_leg = LegPosition(
                    side=PositionSide(side),
                    quantity=quantity,
                    average_price=price if quantity > 0 else Decimal("0"),
                    core_quantity=quantity,
                    core_average_price=price if quantity > 0 else Decimal("0"),
                    tactical_quantity=Decimal("0"),
                    tactical_average_price=Decimal("0"),
                )
                existing_inventory = connection.execute(
                    """SELECT position_json FROM r52_inventory_legs
                       WHERE account_id=? AND symbol=? AND side=?""",
                    (account_id, symbol, side),
                ).fetchone()
                if existing_inventory is not None:
                    existing_leg = _leg_from_dict(json.loads(str(existing_inventory["position_json"])))
                    if existing_leg.quantity != quantity or existing_leg.average_price != adopted_leg.average_price:
                        raise RuntimeError("R5.2 cannot overwrite a divergent inventory during adoption")
                connection.execute(
                    """INSERT INTO r52_inventory_legs(
                        account_id,symbol,side,position_json,last_event_id,updated_at
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account_id,symbol,side) DO UPDATE SET
                        position_json=excluded.position_json,
                        last_event_id=excluded.last_event_id,
                        updated_at=excluded.updated_at""",
                    (
                        account_id,
                        symbol,
                        side,
                        _canonical(_leg_to_dict(adopted_leg)),
                        f"ADOPTION:{manifest_sha256}",
                        now,
                    ),
                )
            connection.commit()

    def core_baseline(self, *, account_id: str, symbol: str) -> Mapping[str, tuple[Decimal, Decimal]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT side,core_quantity,core_average_price
                   FROM r52_position_baseline WHERE account_id=? AND symbol=?""",
                (account_id, symbol),
            ).fetchall()
        return {
            str(row["side"]): (Decimal(str(row["core_quantity"])), Decimal(str(row["core_average_price"])))
            for row in rows
        }

    def inventory_leg(
        self,
        *,
        account_id: str,
        symbol: str,
        side: PositionSide,
    ) -> LegPosition | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT position_json FROM r52_inventory_legs
                   WHERE account_id=? AND symbol=? AND side=?""",
                (account_id, symbol, side.value),
            ).fetchone()
        if row is None:
            return None
        return _leg_from_dict(json.loads(str(row["position_json"])))

    def apply_inventory_event(
        self,
        *,
        event_id: str,
        account_id: str,
        symbol: str,
        side: PositionSide,
        payload: Mapping[str, Any],
        transform: Callable[[LegPosition], LegPosition],
    ) -> tuple[LegPosition, bool]:
        """Apply an exchange fill/funding event exactly once under one DB transaction."""

        self._validate_text(event_id, "event_id", 192)
        canonical = _canonical(payload)
        digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_sha256,result_json FROM r52_inventory_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_sha256"]) != digest:
                    raise RuntimeError("R5.2 inventory event id was reused with different payload")
                result = _leg_from_dict(json.loads(str(existing["result_json"])))
                connection.commit()
                return result, True
            row = connection.execute(
                """SELECT position_json FROM r52_inventory_legs
                   WHERE account_id=? AND symbol=? AND side=?""",
                (account_id, symbol, side.value),
            ).fetchone()
            current = (
                LegPosition(side=side)
                if row is None
                else _leg_from_dict(json.loads(str(row["position_json"])))
            )
            updated = transform(current)
            if updated.side is not side:
                raise RuntimeError("R5.2 inventory transform changed the position side")
            result_json = _canonical(_leg_to_dict(updated))
            connection.execute(
                """INSERT INTO r52_inventory_events(
                    event_id,account_id,symbol,side,payload_sha256,payload_json,result_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (event_id, account_id, symbol, side.value, digest, canonical, result_json, now),
            )
            connection.execute(
                """INSERT INTO r52_inventory_legs(
                    account_id,symbol,side,position_json,last_event_id,updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(account_id,symbol,side) DO UPDATE SET
                    position_json=excluded.position_json,
                    last_event_id=excluded.last_event_id,
                    updated_at=excluded.updated_at""",
                (account_id, symbol, side.value, result_json, event_id, now),
            )
            connection.commit()
        return updated, False

    def register_managed_order(
        self,
        *,
        account_id: str,
        symbol: str,
        planner_intent_id: str,
        client_order_id: str,
        payload: Mapping[str, Any],
        status: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO r52_managed_orders(
                    account_id,symbol,planner_intent_id,client_order_id,status,payload_json,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    planner_intent_id=excluded.planner_intent_id,
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at""",
                (account_id, symbol, planner_intent_id, client_order_id, status, _canonical(payload), now),
            )
            connection.commit()

    def managed_order(
        self,
        reference: str,
        *,
        account_id: str | None = None,
        symbol: str | None = None,
    ) -> Mapping[str, Any] | None:
        clauses = ["(client_order_id=? OR planner_intent_id=?)"]
        params: list[object] = [reference, reference]
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(account_id)
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        sql = (
            "SELECT account_id,symbol,planner_intent_id,client_order_id,status,payload_json "
            "FROM r52_managed_orders WHERE " + " AND ".join(clauses) +
            " ORDER BY updated_at DESC LIMIT 1"
        )
        with self._connect() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
        if row is None:
            return None
        return {
            "account_id": str(row["account_id"]),
            "symbol": str(row["symbol"]),
            "planner_intent_id": str(row["planner_intent_id"]),
            "client_order_id": str(row["client_order_id"]),
            "status": str(row["status"]),
            "payload": json.loads(str(row["payload_json"])),
        }

    def update_managed_order_status(
        self,
        *,
        client_order_id: str,
        status: str,
    ) -> None:
        self._validate_text(client_order_id, "client_order_id", 160)
        self._validate_text(status, "status", 48)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE r52_managed_orders SET status=?,updated_at=? WHERE client_order_id=?",
                (status, now, client_order_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(client_order_id)
            connection.commit()

    def cycle(self, cycle_id: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM r52_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def integrity(self) -> Mapping[str, Any]:
        with self._connect() as connection:
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            full = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign = int(connection.execute("SELECT count(*) FROM pragma_foreign_key_check").fetchone()[0])
        return {
            "quick_check": quick,
            "integrity_check": full,
            "foreign_key_violation_count": foreign,
            "status": "PASS" if quick == "ok" and full == "ok" and foreign == 0 else "FAIL",
        }

    def _transition(self, cycle_id: str, allowed: set[str], target: str, event: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM r52_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            if str(row["status"]) not in allowed:
                raise RuntimeError("R5.2 invalid cycle transition")
            connection.execute(
                "UPDATE r52_cycles SET status=?,updated_at=? WHERE cycle_id=?",
                (target, now, cycle_id),
            )
            self._event(connection, cycle_id, event, {})
            connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("R5.2 strategy store is closed")
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            connection.close()

    def begin_decision_cursor(
        self,
        *,
        account_id: str,
        symbol: str,
        timeframe: str,
        candle_close: datetime,
        decision_id: str,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> bool:
        """Acquire an expiring durable lease for one closed-candle decision."""
        if candle_close.tzinfo is None or candle_close.utcoffset() is None:
            raise ValueError("candle_close must be timezone-aware")
        if not str(decision_id).strip() or not str(lease_owner).strip():
            raise ValueError("decision_id and lease_owner are required")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        close_text = candle_close.astimezone(UTC).isoformat()
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT candle_close,decision_id,status,lease_owner,lease_expires_at "
                "FROM r53_decision_cursor WHERE account_id=? AND symbol=? AND timeframe=?",
                (account_id, symbol, timeframe),
            ).fetchone()
            if row is not None:
                current_close = datetime.fromisoformat(str(row["candle_close"]))
                if candle_close < current_close:
                    connection.commit()
                    return False
                if candle_close == current_close:
                    if str(row["decision_id"]) != str(decision_id):
                        raise RuntimeError("R5 decision cursor conflict on the same candle")
                    if str(row["status"]) == "COMPLETED":
                        connection.commit()
                        return False
                    expiry_raw = row["lease_expires_at"]
                    if (
                        str(row["status"]) == "IN_FLIGHT"
                        and expiry_raw
                        and datetime.fromisoformat(str(expiry_raw)) > now_dt
                        and str(row["lease_owner"] or "") != str(lease_owner)
                    ):
                        connection.commit()
                        return False
            connection.execute(
                """INSERT INTO r53_decision_cursor(
                    account_id,symbol,timeframe,candle_close,decision_id,status,
                    lease_owner,lease_expires_at,updated_at
                ) VALUES(?,?,?,?,?,'IN_FLIGHT',?,?,?)
                ON CONFLICT(account_id,symbol,timeframe) DO UPDATE SET
                    candle_close=excluded.candle_close,
                    decision_id=excluded.decision_id,
                    status='IN_FLIGHT',
                    lease_owner=excluded.lease_owner,
                    lease_expires_at=excluded.lease_expires_at,
                    updated_at=excluded.updated_at""",
                (account_id, symbol, timeframe, close_text, str(decision_id), str(lease_owner), expires, now),
            )
            connection.commit()
        return True

    def complete_decision_cursor(
        self,
        *,
        account_id: str,
        symbol: str,
        timeframe: str,
        decision_id: str,
        lease_owner: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT decision_id,status,lease_owner FROM r53_decision_cursor "
                "WHERE account_id=? AND symbol=? AND timeframe=?",
                (account_id, symbol, timeframe),
            ).fetchone()
            if row is None:
                raise KeyError("decision cursor is not leased")
            if str(row["decision_id"]) != str(decision_id):
                raise RuntimeError("decision cursor identity changed")
            if str(row["status"]) == "COMPLETED":
                connection.commit()
                return
            if str(row["lease_owner"] or "") != str(lease_owner):
                raise PermissionError("decision cursor lease is owned by another worker")
            connection.execute(
                "UPDATE r53_decision_cursor SET status='COMPLETED',lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE account_id=? AND symbol=? AND timeframe=?",
                (now, account_id, symbol, timeframe),
            )
            connection.commit()

    def release_decision_cursor(
        self,
        *,
        account_id: str,
        symbol: str,
        timeframe: str,
        decision_id: str,
        lease_owner: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM r53_decision_cursor WHERE account_id=? AND symbol=? AND timeframe=? "
                "AND decision_id=? AND status='IN_FLIGHT' AND lease_owner=?",
                (account_id, symbol, timeframe, str(decision_id), str(lease_owner)),
            )
            connection.commit()

    def claim_decision_cursor(
        self,
        *,
        account_id: str,
        symbol: str,
        timeframe: str,
        candle_close: datetime,
        decision_id: str,
    ) -> bool:
        """Backward-compatible complete claim for non-writing consumers."""
        owner = f"compat:{decision_id}"
        acquired = self.begin_decision_cursor(
            account_id=account_id,
            symbol=symbol,
            timeframe=timeframe,
            candle_close=candle_close,
            decision_id=decision_id,
            lease_owner=owner,
        )
        if acquired:
            self.complete_decision_cursor(
                account_id=account_id,
                symbol=symbol,
                timeframe=timeframe,
                decision_id=decision_id,
                lease_owner=owner,
            )
        return acquired

    def decision_cursor(
        self, *, account_id: str, symbol: str, timeframe: str
    ) -> Mapping[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT candle_close,decision_id,status,lease_owner,lease_expires_at,updated_at "
                "FROM r53_decision_cursor WHERE account_id=? AND symbol=? AND timeframe=?",
                (account_id, symbol, timeframe),
            ).fetchone()
        if row is None:
            return None
        return {
            "candle_close": str(row["candle_close"]),
            "decision_id": str(row["decision_id"]),
            "status": str(row["status"]),
            "lease_owner": str(row["lease_owner"] or ""),
            "lease_expires_at": str(row["lease_expires_at"] or ""),
            "updated_at": str(row["updated_at"]),
        }

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS r52_cycles(
                    cycle_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_sha256 TEXT,
                    plan_json TEXT,
                    report_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS r52_cycle_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES r52_cycles(cycle_id)
                );
                CREATE TABLE IF NOT EXISTS r52_leg_state(
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol,side),
                    FOREIGN KEY(cycle_id) REFERENCES r52_cycles(cycle_id)
                );
                CREATE TABLE IF NOT EXISTS r52_position_baseline(
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    core_quantity TEXT NOT NULL,
                    core_average_price TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol,side)
                );
                CREATE TABLE IF NOT EXISTS r52_inventory_legs(
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    position_json TEXT NOT NULL,
                    last_event_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol,side)
                );
                CREATE TABLE IF NOT EXISTS r52_inventory_events(
                    event_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_r52_inventory_events_scope
                    ON r52_inventory_events(account_id,symbol,side,created_at);
                CREATE TABLE IF NOT EXISTS r52_managed_orders(
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    planner_intent_id TEXT NOT NULL,
                    client_order_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_r52_managed_orders_planner
                    ON r52_managed_orders(account_id,symbol,planner_intent_id,updated_at);
                CREATE TABLE IF NOT EXISTS r53_decision_cursor(
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_close TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'COMPLETED',
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,symbol,timeframe)
                );
                """
            )
            cursor_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(r53_decision_cursor)").fetchall()
            }
            for column, definition in (
                ("status", "TEXT NOT NULL DEFAULT 'COMPLETED'"),
                ("lease_owner", "TEXT"),
                ("lease_expires_at", "TEXT"),
            ):
                if column not in cursor_columns:
                    connection.execute(
                        f"ALTER TABLE r53_decision_cursor ADD COLUMN {column} {definition}"
                    )

    @staticmethod
    def _event(connection: sqlite3.Connection, cycle_id: str, event: str, details: Mapping[str, Any]) -> None:
        connection.execute(
            "INSERT INTO r52_cycle_events(cycle_id,event,details_json,created_at) VALUES(?,?,?,?)",
            (cycle_id, event, _canonical(details), datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def _validate_text(value: str, name: str, limit: int) -> None:
        text = str(value).strip()
        if not text or len(text) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise ValueError(f"{name} is invalid")
