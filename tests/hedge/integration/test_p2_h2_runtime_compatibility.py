from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from freqtrade.enums import PositionAction, PositionMode, PositionSide
import freqtrade.hedge as hedge_public
from freqtrade.hedge.binance_readonly import BinanceReadonlyAdapter
from freqtrade.hedge.config import HedgeRuntimeConfig
from freqtrade.hedge.contracts.ports import InMemoryPositionLock, InMemorySingleWriter, SystemClock
from freqtrade.hedge.domain import HedgeAction, PositionKey
from freqtrade.hedge.position_book import PositionRecord
from freqtrade.hedge.risk.engine import HedgeRiskEngine
from freqtrade.hedge.risk.limits import RiskLimits
from freqtrade.hedge.risk.models import AccountRiskSnapshot
from freqtrade.hedge.reconciliation import reconcile_positions
from freqtrade.persistence.base import ModelBase
from freqtrade.persistence.hedge_models import HedgeModelBase


class FullAccountFakeApi:
    def fapiPrivateGetPositionSideDual(self):
        return {"dualSidePosition": True}

    def fetch_time(self):
        return int(datetime.now(UTC).timestamp() * 1000)

    def fetch_positions(self, symbols=None, params=None):
        del symbols, params
        return [
            {
                "symbol": "ETHUSDT",
                "positionSide": "LONG",
                "positionAmt": "0.5",
                "entryPrice": "3000",
                "markPrice": "3010",
                "liquidationPrice": "1500",
                "leverage": "3",
                "initialMargin": "500",
                "unRealizedProfit": "5",
                "marginType": "cross",
                "updateTime": 123456,
            },
            {
                "symbol": "BTCUSDT",
                "positionSide": "SHORT",
                "positionAmt": "-0.01",
                "entryPrice": "70000",
                "markPrice": "69900",
                "liquidationPrice": "90000",
                "leverage": "3",
                "initialMargin": "233",
                "unRealizedProfit": "1",
                "marginType": "cross",
                "updateTime": 123457,
            },
        ]

    def fetch_open_orders(self, symbol=None, params=None):
        del symbol, params
        return [{"symbol": "XRPUSDT", "id": "unmanaged-order"}]

    def fetch_my_trades(self, symbol=None, params=None):
        del symbol, params
        return []

    def fetch_balance(self, params=None):
        del params
        return {
            "info": {
                "totalWalletBalance": "1000",
                "totalMarginBalance": "1010",
                "availableBalance": "500",
                "totalInitialMargin": "300",
                "totalMaintMargin": "10",
            }
        }


def rich_config() -> HedgeRuntimeConfig:
    return HedgeRuntimeConfig(
        position_mode=PositionMode.HEDGE,
        enabled=True,
        managed_pair="ETH/USDT:USDT",
        account_id="main",
        exchange_adapter="binance",
        read_only=True,
        live_trading_enabled=False,
        target_leverage=Decimal("3"),
        max_clock_skew_ms=5000,
        user_stream_max_age_ms=60000,
        rest_reconcile_interval_seconds=60,
        max_gross_notional=Decimal("10000"),
        max_gross_exposure_ratio=Decimal("2"),
        max_margin_utilization=Decimal("0.55"),
        min_liquidation_buffer_ratio=Decimal("0.20"),
    )


def test_rich_runtime_config_and_full_account_read_are_preserved() -> None:
    adapter = BinanceReadonlyAdapter(FullAccountFakeApi(), rich_config())
    check = adapter.check_account([])
    codes = {issue.code for issue in check.issues}

    assert len(check.snapshot.positions) == 2
    assert check.snapshot.open_orders
    assert "UNMANAGED_POSITION" in codes
    assert "UNMANAGED_ORDER" in codes
    assert check.snapshot.positions[0].account_id == "main"
    payload = check.to_dict()
    assert payload["managed_pair"] == "ETH/USDT:USDT"
    assert payload["issues"] == payload["safety_issues"]


def test_account_side_metadata_and_reconciliation_tolerance_are_preserved() -> None:
    key = PositionKey(
        exchange="binance",
        account_id="main",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
    )
    action = HedgeAction(
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        position_action=PositionAction.INCREASE,
        quantity="0.1",
        account_id="main",
        strategy_id="grid-v1",
    )
    record = PositionRecord(
        exchange="binance",
        account_id="main",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        amount="0.5",
        entry_price="3000",
        mark_price="3010",
        liquidation_price="1500",
        leverage="3",
        margin_mode="cross",
        source_time_ms=123456,
        version=7,
    )
    result = reconcile_positions([record], [record], amount_tolerance="0.0001")

    assert key.identity == ("binance", "main", "ETH/USDT:USDT", PositionSide.LONG)
    assert action.account_id == "main"
    assert action.strategy_id == "grid-v1"
    assert record.reference_price == Decimal("3010")
    assert record.notional == Decimal("1505.0")
    assert result.amount_tolerance == Decimal("0.0001")
    assert result.consistent


def test_legacy_port_and_orm_names_remain_available() -> None:
    writer = InMemorySingleWriter(leader=True)
    clock = SystemClock()

    assert writer.claim(account_id="main", owner_id="worker-1")
    writer.assert_leader(account_id="main", now=clock.now())
    assert isinstance(clock.now_ms(), int)
    key = __import__("freqtrade.hedge.contracts", fromlist=["PositionKey"]).PositionKey(
        exchange="binance", account_id="main", canonical_symbol="ETH/USDT:USDT", position_side="LONG"
    )
    lock = InMemoryPositionLock()
    assert lock.acquire(key=key, owner_id="worker-1") is True
    assert lock.acquire(key=key, owner_id="worker-2") is False
    assert HedgeModelBase is not ModelBase
    assert HedgeModelBase.metadata is not ModelBase.metadata
    assert all(name.startswith("hedge_") for name in HedgeModelBase.metadata.tables)


def test_legacy_package_root_exports_are_not_dropped() -> None:
    expected = {
        "HedgeConfigurationError",
        "HedgeDataError",
        "HedgeError",
        "HedgeInvariantError",
        "HedgeSafetyError",
        "ReduceOnlyDecision",
        "calculate_safe_reduce",
        "canonicalize_symbol",
        "raw_symbol",
        "symbols_equivalent",
        "AccountRiskSnapshot",
        "HedgeRiskEngine",
        "RiskDecision",
        "RiskLimits",
        "PendingOrder",
        "TargetDelta",
        "TargetPosition",
        "calculate_target_delta",
        "target_idempotency_key",
        "BinanceUserStreamLifecycle",
        "UserStreamState",
        "UserStreamStatus",
    }
    assert expected <= set(hedge_public.__all__)
    assert hedge_public.HedgeDataError.__name__ == "HedgeDataError"
    assert hedge_public.ReduceOnlyDecision.__name__ == "SafeReduceResult"


def _compat_account() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        account_id="main",
        equity=Decimal("1000"),
        wallet_balance=Decimal("1000"),
        available_balance=Decimal("1"),
        initial_margin=Decimal("950"),
        maintenance_margin=Decimal("10"),
        gross_long_notional=Decimal("100"),
        gross_short_notional=Decimal("0"),
        net_notional=Decimal("100"),
    )


def _compat_limits() -> RiskLimits:
    return RiskLimits(
        max_margin_utilization=Decimal("0.50"),
        min_liquidation_buffer_ratio=Decimal("0.10"),
        max_gross_notional=Decimal("1000"),
    )


def test_legacy_unscoped_reduce_remains_margin_independent() -> None:
    engine = HedgeRiskEngine(_compat_limits())
    decision = engine.evaluate(
        action=PositionAction.REDUCE,
        requested_quantity=Decimal("0.1"),
        reference_price=Decimal("1000"),
        account=_compat_account(),
    )

    assert decision.allowed
    assert decision.approved_quantity == Decimal("0.1")


def test_modern_side_aware_reduce_still_requires_confirmed_quantity() -> None:
    engine = HedgeRiskEngine(_compat_limits())
    decision = engine.evaluate(
        action=PositionAction.REDUCE,
        requested_quantity=Decimal("0.1"),
        reference_price=Decimal("1000"),
        account=_compat_account(),
        position_side=PositionSide.LONG,
    )

    assert not decision.allowed
    assert decision.reason_codes == ("CONFIRMED_POSITION_REQUIRED",)
