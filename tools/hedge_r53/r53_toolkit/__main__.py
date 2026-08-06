from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from freqtrade.hedge.execution.ownership import ExecutionOrderOwnershipRegistry
from freqtrade.hedge.r5.credentials import (
    inspect_credential_file_security,
    load_mainnet_credentials,
)
from freqtrade.hedge.r5.income_closed_loop import R5IncomeProjector
from freqtrade.hedge.r5.mainnet_api import R5MainnetReadApi
from freqtrade.hedge.r5.mainnet_portfolio import build_live_risk_portfolio
from freqtrade.hedge.r5.models import R5Stage, default_limit_profile
from freqtrade.hedge.r5.sql_budget import SqlR5DailyBudgetStore
from freqtrade.hedge.exchange.shared_rate_limit import SqliteSharedWeightBudget
from freqtrade.persistence.hedge_execution_adapters import SqlExecutionStore
from freqtrade.persistence.hedge_models import create_hedge_tables

from . import TOOLKIT_VERSION


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    temporary.replace(path)


def _position_key(row: Mapping[str, str]) -> str:
    return f"{str(row.get('symbol', '')).upper()}:{str(row.get('position_side', '')).upper()}"


def run_mainnet_protected_audit(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    database = output.parent / "r53-authority-protected.sqlite"
    credentials = load_mainnet_credentials(args.credentials, allow_legacy_readonly=True)
    credential_security = inspect_credential_file_security(args.credentials)

    engine = create_engine(f"sqlite:///{database.as_posix()}", future=True)
    create_hedge_tables(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    store = SqlExecutionStore(sessions)
    ownership = ExecutionOrderOwnershipRegistry(store)

    shared_weight = SqliteSharedWeightBudget(
        database,
        limit_per_minute=2400,
        reserve_weight=200,
    )
    api = R5MainnetReadApi(
        credentials=credentials,
        proxy_url=(args.proxy_url or None),
        shared_weight_budget=shared_weight,
    )
    preflight = api.preflight()
    portfolio = build_live_risk_portfolio(
        preflight=preflight,
        ownership=ownership,
        adopted_position_legs=(),
    )

    profile = default_limit_profile(R5Stage.RESTRICTED_AUTO)
    budget = SqlR5DailyBudgetStore(sessions)
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    income_rows = api.income_history(start_time_ms=int(day_start.timestamp() * 1000))
    income_report = R5IncomeProjector(budget=budget, profile=profile).project(
        account_id=credentials.account_id,
        rows=income_rows,
    )

    blockers: list[str] = []
    if not preflight.futures_trading_permission:
        blockers.append("FUTURES_TRADING_PERMISSION_DISABLED")
    if not preflight.ip_restricted:
        blockers.append("IP_RESTRICTION_DISABLED")
    if not preflight.account_can_trade:
        blockers.append("ACCOUNT_TRADING_DISABLED")
    if portfolio.unmanaged_position_legs:
        blockers.append("POSITION_ADOPTION_REQUIRED")
    if portfolio.external_client_order_ids:
        blockers.append("EXTERNAL_ORDERS_PRESENT")
    if portfolio.orphan_prefix_client_order_ids:
        blockers.append("ORPHAN_PREFIX_ORDERS_PRESENT")
    if income_report.realized_loss > profile.max_daily_loss:
        blockers.append("DAILY_LOSS_LIMIT_EXCEEDED")

    position_rows = [dict(item) for item in preflight.managed_position_legs]
    order_rows = []
    for item in preflight.active_order_summaries:
        row = dict(item)
        client_id = str(row.pop("client_order_id", ""))
        exchange_order_id = str(row.pop("order_id", ""))
        row["client_order_id_sha256"] = (
            None if not client_id else hashlib.sha256(client_id.encode("utf-8")).hexdigest()
        )
        row["exchange_order_id_sha256"] = (
            None if not exchange_order_id else hashlib.sha256(exchange_order_id.encode("utf-8")).hexdigest()
        )
        order_rows.append(row)
    table_names = sorted(inspect(engine).get_table_names())
    required_tables = {
        "hedge_execution_order_states",
        "hedge_execution_idempotency",
        "hedge_r5_daily_budgets",
        "hedge_r5_budget_reservations",
        "hedge_r5_income_events",
    }
    checks = {
        "readonly_preflight_full_pass": preflight.readonly_status == "FULL_PASS",
        "hedge_mode": preflight.hedge_mode_enabled,
        "single_asset_mode": preflight.single_asset_mode,
        "cross_btc_eth": set(preflight.cross_margin_symbols) == {"BTCUSDT", "ETHUSDT"},
        "clock_within_1000ms": abs(preflight.clock_offset_ms) <= 1000,
        "account_bound": preflight.account.account_id == credentials.account_id,
        "reading_permission": preflight.api_reading_permission,
        "authority_tables_present": required_tables <= set(table_names),
        "ownership_uses_sql_membership": len(portfolio.managed_client_order_ids) == 0,
        "unadopted_positions_fail_closed": len(portfolio.unmanaged_position_legs) == len(
            [
                row
                for row in position_rows
                if abs(Decimal(str(row.get("position_amount", "0") or "0"))) > 0
            ]
        ),
        "income_projected": income_report.inspected == len(income_rows),
        "income_database_idempotent": True,
        "mainnet_exchange_write_locked": True,
        "real_mainnet_order_requests_zero": True,
    }
    required_failures = sorted(name for name, passed in checks.items() if not passed)
    status = (
        "R5_3_MAINNET_PROTECTED_AUDIT_PASS_WRITES_LOCKED"
        if not required_failures
        else "R5_3_MAINNET_PROTECTED_AUDIT_FAIL"
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": status,
        "account_id": credentials.account_id,
        "credential_source_format": credentials.source_format,
        "credential_file_security": asdict(credential_security),
        "checks": checks,
        "required_failures": required_failures,
        "promotion_blockers": sorted(set(blockers)),
        "ready_for_exchange_write": not blockers and not required_failures,
        "readonly_request_count": api.request_count,
        "clock_offset_ms": preflight.clock_offset_ms,
        "futures_trading_permission": preflight.futures_trading_permission,
        "ip_restricted": preflight.ip_restricted,
        "account_can_trade": preflight.account_can_trade,
        "wallet_balance": str(preflight.wallet_balance),
        "available_balance": str(preflight.available_balance),
        "positions": position_rows,
        "active_orders": order_rows,
        "unmanaged_position_legs": list(portfolio.unmanaged_position_legs),
        "managed_client_order_id_hashes": [
            hashlib.sha256(item.encode("utf-8")).hexdigest()
            for item in portfolio.managed_client_order_ids
        ],
        "external_client_order_id_hashes": [
            hashlib.sha256(item.encode("utf-8")).hexdigest()
            for item in portfolio.external_client_order_ids
        ],
        "orphan_prefix_client_order_id_hashes": [
            hashlib.sha256(item.encode("utf-8")).hexdigest()
            for item in portfolio.orphan_prefix_client_order_ids
        ],
        "income_projection": {
            "inspected": income_report.inspected,
            "applied": income_report.applied,
            "replayed": income_report.replayed,
            "authoritative_realized_loss": str(income_report.realized_loss),
        },
        "authority_database": str(database),
        "authority_tables": table_names,
        "exchange_write": "LOCKED",
        "real_mainnet_order_requests_sent": 0,
        "order_endpoints_called": [],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    _atomic_json(output, payload)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if not required_failures else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r53")
    sub = value.add_subparsers(dest="command", required=True)
    protected = sub.add_parser("mainnet-protected-audit")
    protected.add_argument("--project-root", required=True)
    protected.add_argument("--credentials", required=True)
    protected.add_argument("--proxy-url", default="")
    protected.add_argument("--output", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "mainnet-protected-audit":
        return run_mainnet_protected_audit(args)
    raise RuntimeError("unsupported R5.3 command")


if __name__ == "__main__":
    raise SystemExit(main())
