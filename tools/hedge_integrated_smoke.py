from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.getcwd())

from freqtrade.enums.hedge import PositionMode
from freqtrade.hedge.config import HedgeRuntimeConfig
from freqtrade.hedge.integration import IntegratedPaperHedgeApplication
from freqtrade.hedge.planning.context import MarketSnapshot
from freqtrade.hedge.runtime import HedgeRuntime
from freqtrade.hedge.readonly import runtime_config_from_freqtrade


def market(price: str, step: int) -> MarketSnapshot:
    mark = Decimal(price)
    return MarketSnapshot(
        symbol="ETH/USDT:USDT",
        timestamp=datetime.now(UTC) + timedelta(seconds=step),
        bid=mark - Decimal("0.10"),
        ask=mark + Decimal("0.10"),
        mark=mark,
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def main() -> int:
    readonly_config = runtime_config_from_freqtrade(
        {
            "exchange": {
                "key": "smoke-key",
                "secret": "smoke-secret",
                "pair_whitelist": ["ETH/USDT:USDT"],
            },
            "hedge": {
                "max_clock_skew_ms": 5000,
                "target_leverage": "3",
            },
        }
    )
    if readonly_config.max_clock_skew_ms != 5000 or readonly_config.target_leverage != 3:
        raise RuntimeError("readonly runtime configuration bridge did not apply clock/leverage")

    config = {
        "hedge": {
            "target_leverage": "3",
            "max_gross_notional": "1000",
            "max_gross_exposure_ratio": "1.0",
            "max_margin_utilization": "0.80",
            "min_liquidation_buffer_ratio": "0.05",
            "max_single_order_notional": "250",
            "paper": {
                "initial_balance": "1000",
                "auto_fill": True,
                "long_signal": "1",
                "short_signal": "1",
            },
            "planner": {
                "cooldown_seconds": 0,
                "max_grid_layers": 3,
                "core_wallet_exposure_long": "0.05",
                "tactical_wallet_exposure_long": "0.10",
                "tactical_wallet_exposure_short": "0.10",
            },
        }
    }
    app = IntegratedPaperHedgeApplication(
        config=config,
        account_id="hedge-main",
        symbol="ETH/USDT:USDT",
    )
    cycles = [
        app.run_market_cycle(market("2000", 0)),
        app.run_market_cycle(market("1980", 1)),
        app.run_market_cycle(market("2010", 2)),
    ]
    runtime = HedgeRuntime(
        HedgeRuntimeConfig(
            position_mode=PositionMode.HEDGE,
            enabled=True,
            managed_pair="ETH/USDT:USDT",
            exchange_adapter="binance",
            read_only=True,
            account_id="hedge-main",
            live_trading_enabled=False,
        )
    )
    app.publish_runtime(runtime)
    view = runtime.view()
    wallet = app.wallet()
    result = {
        "cycles": len(cycles),
        "planned": sum(len(item.planning.submit_orders) for item in cycles),
        "submitted": sum(len(item.executions) for item in cycles),
        "filled": sum(len(item.fills) for item in cycles),
        "long_quantity": str(wallet.long.quantity),
        "long_average": str(wallet.long.average_price),
        "short_quantity": str(wallet.short.quantity),
        "short_average": str(wallet.short.average_price),
        "equity": str(wallet.equity),
        "runtime_positions": len(view.positions),
        "runtime_risk_valid": bool(view.risk and view.risk.effective_risk_data_valid),
        "runtime_ready": view.ready,
        "readonly_max_clock_skew_ms": readonly_config.max_clock_skew_ms,
        "readonly_target_leverage": readonly_config.target_leverage,
    }
    if (
        result["filled"] <= 0
        or wallet.long.quantity <= 0
        or wallet.short.quantity <= 0
        or len(view.positions) != 2
        or not result["runtime_risk_valid"]
    ):
        raise RuntimeError(f"integrated paper main-path smoke failed: {result}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
