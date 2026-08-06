from datetime import UTC, datetime
from decimal import Decimal

import pytest

from freqtrade.enums.hedge import PositionMode, PositionSide
from freqtrade.exceptions import OperationalException
from freqtrade.hedge.binance_readonly import (
    BinanceReadonlyAdapter,
    parse_binance_position,
)
from freqtrade.hedge.config import HedgeRuntimeConfig


def config() -> HedgeRuntimeConfig:
    return HedgeRuntimeConfig(
        position_mode=PositionMode.HEDGE,
        enabled=True,
        managed_pair="ETH/USDT:USDT",
        account_id="main",
        exchange_adapter="binance",
        read_only=True,
        live_trading_enabled=False,
        target_leverage=None,
        max_clock_skew_ms=5000,
        user_stream_max_age_ms=60000,
        rest_reconcile_interval_seconds=60,
        max_gross_notional=None,
        max_gross_exposure_ratio=None,
        max_margin_utilization=Decimal("0.55"),
        min_liquidation_buffer_ratio=Decimal("0.20"),
    )


class FakeApi:
    def __init__(self):
        self.positions_symbols = "not-called"

    def fapiPrivateGetPositionSideDual(self):
        return {"dualSidePosition": True}

    def fetch_positions(self, symbols=None, params=None):
        self.positions_symbols = symbols
        return [
            {
                "symbol": "ETHUSDT",
                "positionSide": "LONG",
                "positionAmt": "1",
                "marginType": "cross",
            },
            {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "positionAmt": "0.01",
                "marginType": "cross",
            },
        ]

    def fetch_balance(self, params=None):
        return {
            "totalWalletBalance": "10000",
            "totalMarginBalance": "10000",
            "availableBalance": "9000",
            "totalInitialMargin": "1000",
            "totalMaintMargin": "100",
        }

    def fetch_open_orders(self, symbol=None, params=None):
        return []

    def fetch_my_trades(self, symbol=None, params=None):
        return []

    def fetch_time(self):
        return int(datetime.now(UTC).timestamp() * 1000)


def test_account_check_fetches_all_positions_and_halts_unmanaged() -> None:
    api = FakeApi()
    adapter = BinanceReadonlyAdapter(api, config())
    check = adapter.check_account([])
    assert api.positions_symbols is None
    assert "UNMANAGED_POSITION" in {item.code for item in check.issues}


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "ETHUSDT", "positionSide": "LONG", "positionAmt": "-1"},
        {"symbol": "ETHUSDT", "positionSide": "SHORT", "positionAmt": "1"},
        {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "1"},
        {"symbol": "ETHUSDT", "positionSide": "LONG", "positionAmt": "NaN"},
    ],
)
def test_invalid_side_sign_or_number_is_rejected(payload) -> None:
    with pytest.raises(OperationalException):
        parse_binance_position(payload, managed_pair="ETH/USDT:USDT")


def test_raw_symbol_is_mapped_to_managed_pair() -> None:
    record = parse_binance_position(
        {
            "symbol": "ETHUSDT",
            "positionSide": "SHORT",
            "positionAmt": "-2",
            "marginType": "cross",
        },
        managed_pair="ETH/USDT:USDT",
    )
    assert record.symbol == "ETH/USDT:USDT"
    assert record.position_side is PositionSide.SHORT
