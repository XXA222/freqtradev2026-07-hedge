from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from freqtrade.hedge.integration.paper_runtime import IntegratedPaperHedgeApplication
from freqtrade.hedge.planning.context import MarketSnapshot
from freqtrade.hedge.simulation.exchange import BarEvent


def config() -> dict[str, object]:
    return {
        "managed_pair": "ETH/USDT:USDT",
        "hedge": {
            "target_leverage": "3",
            "max_gross_notional": "10000",
            "max_gross_exposure_ratio": "0.80",
            "max_margin_utilization": "0.80",
            "max_single_order_notional": "100",
            "min_liquidation_buffer_ratio": "0.05",
            "planner": {"cooldown_seconds": 0, "max_grid_layers": 2},
            "paper": {
                "initial_balance": "1000",
                "auto_fill": True,
                "long_signal": "1",
                "short_signal": "1",
                "tick_size": "0.1",
                "qty_step": "0.001",
                "min_qty": "0.001",
                "min_notional": "5",
                "bar_volume": "100",
            },
        },
    }


def market(ts: datetime, value: str = "3000") -> MarketSnapshot:
    price = Decimal(value)
    return MarketSnapshot(
        symbol="ETH/USDT:USDT",
        timestamp=ts,
        bid=price - Decimal("1"),
        ask=price + Decimal("1"),
        mark=price,
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def bar(item: MarketSnapshot) -> BarEvent:
    return BarEvent(
        timestamp=item.timestamp,
        symbol=item.symbol,
        open=item.mark,
        high=item.ask,
        low=item.bid,
        close=item.mark,
        volume=Decimal("100"),
    )


def test_stop_new_risk_blocks_entries_without_advancing_blocked_side_state():
    app = IntegratedPaperHedgeApplication(
        config=config(),
        account_id="binance-usdm:r34-paper",
        symbol="ETH/USDT:USDT",
    )
    app.bind_new_risk_provider(lambda: False)
    first_market = market(datetime(2026, 8, 3, tzinfo=UTC))
    stopped = app.run_market_cycle(first_market, bar=bar(first_market))
    assert stopped.executions == ()
    assert any(item.startswith("NEW_RISK_STOPPED:") for item in stopped.planning.diagnostics)
    assert app.long_state.sequence == 0
    assert app.short_state.sequence == 0

    app.bind_new_risk_provider(lambda: True)
    resumed_market = market(first_market.timestamp + timedelta(minutes=1))
    resumed = app.run_market_cycle(resumed_market, bar=bar(resumed_market))
    assert resumed.executions
