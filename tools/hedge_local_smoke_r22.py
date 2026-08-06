"""Offline deterministic R2.2 smoke for local Windows validation.

No exchange credentials or network calls are used. The smoke exercises the
same planner/matcher, SQL execution store, idempotency, fill ledger, checkpoint,
and restart recovery used by durable Hedge Paper.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.integration.paper_events import SqlPaperExecutionRecovery
from freqtrade.hedge.integration.paper_runtime import IntegratedPaperHedgeApplication
from freqtrade.hedge.integration.paper_state import SqlPaperStateStore
from freqtrade.hedge.planning.context import MarketSnapshot
from freqtrade.hedge.simulation.exchange import BarEvent
from freqtrade.persistence.hedge_execution_adapters import (
    SqlExecutionIdempotencyStore,
    SqlExecutionLedger,
    SqlExecutionStore,
)
from freqtrade.persistence.hedge_models import HedgeModelBase

PAIR = "ETH/USDT:USDT"
RAW_PAIR = "ETHUSDT"
ACCOUNT = "paper-r22-smoke"
START = datetime(2026, 8, 1, 0, 5, tzinfo=UTC)


def _config() -> dict[str, object]:
    return {
        "managed_pair": PAIR,
        "hedge": {
            "paper": {
                "ephemeral": True,
                "initial_balance": "1000",
                "leverage": "3",
                "auto_fill": True,
                "funding_source": "none",
                "account_events_enabled": False,
                "maker_fee_rate": "0.0002",
                "taker_fee_rate": "0.0004",
                "volume_participation": "1",
                "max_fill_ratio_per_order": "1",
                "bar_volume": "10000",
            },
            "planner": {
                "short_enabled": False,
                "core_wallet_exposure_long": "0.20",
                "tactical_wallet_exposure_long": "0",
                "initial_entry_fraction": "1",
                "max_grid_layers": 1,
                "cooldown_seconds": 0,
                "trailing_rebound": "0",
            },
        },
    }


def _market(ts: datetime, price: str) -> MarketSnapshot:
    mark = Decimal(price)
    return MarketSnapshot(
        symbol=PAIR,
        timestamp=ts,
        bid=mark - Decimal("0.1"),
        ask=mark + Decimal("0.1"),
        mark=mark,
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def _bar(ts: datetime, price: str) -> BarEvent:
    value = Decimal(price)
    return BarEvent(
        timestamp=ts,
        symbol=PAIR,
        open=value,
        high=value + Decimal("5"),
        low=value - Decimal("5"),
        close=value,
        volume=Decimal("10000"),
    )


def _runtime(sessions, owner: str):
    store = SqlExecutionStore(sessions)
    return build_integrated_fake_runtime(
        store=store,
        idempotency=SqlExecutionIdempotencyStore(
            sessions, store, owner_id=owner
        ),
        transaction=SqlExecutionLedger(sessions),
    )


def _application(sessions, owner: str) -> IntegratedPaperHedgeApplication:
    return IntegratedPaperHedgeApplication(
        config=_config(),
        account_id=ACCOUNT,
        symbol=PAIR,
        execution_runtime=_runtime(sessions, owner),
        state_store=SqlPaperStateStore(
            sessions,
            exchange="paper",
            account_id=ACCOUNT,
            symbol=PAIR,
            source="PAPER",
        ),
        execution_recovery=SqlPaperExecutionRecovery(
            sessions, account_id=ACCOUNT, symbol=RAW_PAIR
        ),
    )


def run(database: Path, output: Path) -> dict[str, object]:
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    HedgeModelBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        first = _application(sessions, "r22-smoke-a")
        submitted = first.run_market_cycle(
            _market(START, "100"), bar=_bar(START, "100")
        )
        if not submitted.executions or submitted.fills:
            raise RuntimeError("first candle must submit without same-bar fill")
        second_time = START + timedelta(minutes=5)
        filled = first.run_market_cycle(
            _market(second_time, "100"), bar=_bar(second_time, "100")
        )
        if not filled.fills:
            raise RuntimeError("second candle must execute prior accepted order")
        before = first.wallet(_market(second_time, "100"))

        restarted = _application(sessions, "r22-smoke-b")
        after = restarted.wallet(_market(second_time, "100"))
        if restarted.last_market is None or restarted.last_market.timestamp != second_time:
            raise RuntimeError("durable candle cursor did not recover")
        if before.long.quantity != after.long.quantity:
            raise RuntimeError("SQL fill recovery changed long quantity")
        if before.long.core_quantity != after.long.core_quantity:
            raise RuntimeError("SQL fill recovery changed bucket allocation")
        if before.balance != after.balance:
            raise RuntimeError("SQL fill recovery changed balance/fees")

        recovered_cursor = restarted.last_market.timestamp.isoformat()
        third_time = second_time + timedelta(minutes=5)
        third = restarted.run_market_cycle(
            _market(third_time, "101"), bar=_bar(third_time, "101")
        )
        result = {
            "schema_version": "hedge-local-smoke-r22-v1",
            "status": "PASS",
            "database": str(database),
            "first_submissions": len(submitted.executions),
            "second_fills": len(filled.fills),
            "third_fills": len(third.fills),
            "recovered_cursor": recovered_cursor,
            "final_cursor": restarted.last_market.timestamp.isoformat()
            if restarted.last_market is not None
            else None,
            "long_quantity": str(restarted.wallet().long.quantity),
            "long_core_quantity": str(restarted.wallet().long.core_quantity),
            "balance": str(restarted.wallet().balance),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return result
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.database.resolve(), args.output.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
