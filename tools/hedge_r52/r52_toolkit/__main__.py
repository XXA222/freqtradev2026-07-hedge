from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from freqtrade.hedge.planning.context import PlannerConfig
from freqtrade.hedge.r5.credentials import load_mainnet_credentials
from freqtrade.hedge.r5.mainnet_api import R5MainnetReadApi
from freqtrade.hedge.r5.models import R5Stage, default_limit_profile
from freqtrade.hedge.r5.strategy_runtime import (
    R5PositionPolicy,
    R5ProductionStrategyRuntime,
    R5StrategyMode,
    R5StrategySignals,
)
from freqtrade.hedge.r5.strategy_shadow import facts_from_preflight
from freqtrade.hedge.r5.strategy_store import R5StrategyStateStore


def _planner_config() -> PlannerConfig:
    return PlannerConfig(
        core_wallet_exposure_long=Decimal("0.03"),
        core_wallet_exposure_short=Decimal("0.03"),
        tactical_wallet_exposure_long=Decimal("0.02"),
        tactical_wallet_exposure_short=Decimal("0.02"),
        max_wallet_exposure_long=Decimal("0.08"),
        max_wallet_exposure_short=Decimal("0.08"),
        max_gross_wallet_exposure=Decimal("0.10"),
        initial_entry_fraction=Decimal("0.25"),
        max_grid_layers=4,
        grid_spacing=Decimal("0.01"),
        grid_spacing_growth=Decimal("1.20"),
        grid_qty_growth=Decimal("1.15"),
        trailing_rebound=Decimal("0.003"),
        take_profit_spacing=Decimal("0.008"),
        take_profit_layers=3,
        tactical_reduce_fraction=Decimal("0.34"),
        core_min_fraction=Decimal("0.75"),
        cooldown_seconds=60,
        max_pending_entries=4,
        max_single_order_notional=Decimal("25"),
    )


def run_shadow(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    credentials = load_mainnet_credentials(args.credentials, allow_legacy_readonly=True)
    api = R5MainnetReadApi(credentials=credentials, proxy_url=args.proxy_url or None)
    preflight = api.preflight()
    store = R5StrategyStateStore(output.parent / "r52-strategy-state.sqlite")
    reports = []
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for symbol in ("BTCUSDT", "ETHUSDT"):
        facts = facts_from_preflight(preflight, symbol=symbol, store=store)
        runtime = R5ProductionStrategyRuntime(
            account_id=credentials.account_id,
            symbol=symbol,
            mode=R5StrategyMode.MAINNET_LOCKED,
            profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
            planner_config=_planner_config(),
            store=store,
            position_policy=R5PositionPolicy.PROTECT_AS_CORE,
        )
        result = runtime.run_cycle(
            cycle_id=f"r52-shadow-{stamp}-{symbol.lower()}",
            facts=facts,
            signals=R5StrategySignals(
                long_signal=Decimal(str(args.long_signal)),
                short_signal=Decimal(str(args.short_signal)),
                target_net_quantity=(
                    None if args.target_net_quantity is None else Decimal(str(args.target_net_quantity))
                ),
                realized_volatility=Decimal(str(args.realized_volatility)),
                funding_rate=Decimal(str(args.funding_rate)),
            ),
        )
        reports.append(result.report)
    payload = {
        "schema_version": 1,
        "toolkit_version": "5.2.3",
        "status": "R5_2_PRODUCTION_STRATEGY_CORE_SHADOW_PASS",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "account_id": credentials.account_id,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "preflight_read_requests": preflight.request_count,
        "exchange_write": "LOCKED",
        "real_mainnet_order_requests_sent": 0,
        "cycles": reports,
        "state_database": str(store.path),
        "database_integrity": store.integrity(),
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="ascii")
    temporary.replace(output)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r52")
    sub = value.add_subparsers(dest="command", required=True)
    shadow = sub.add_parser("mainnet-shadow")
    shadow.add_argument("--project-root", required=True)
    shadow.add_argument("--credentials", required=True)
    shadow.add_argument("--proxy-url", default="")
    shadow.add_argument("--output", required=True)
    shadow.add_argument("--long-signal", default="0")
    shadow.add_argument("--short-signal", default="0")
    shadow.add_argument("--target-net-quantity")
    shadow.add_argument("--realized-volatility", default="0")
    shadow.add_argument("--funding-rate", default="0")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "mainnet-shadow":
        return run_shadow(args)
    raise RuntimeError("unsupported R5.2 command")


if __name__ == "__main__":
    raise SystemExit(main())
