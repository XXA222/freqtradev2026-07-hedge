from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import ccxt
import pytest

from freqtrade.enums import PositionMode, PositionSide
from freqtrade.exceptions import DDosProtection, OperationalException, TemporaryError
from freqtrade.hedge.binance_readonly import (
    BinanceReadonlyAdapter,
    BinanceReadonlyState,
    parse_binance_account_risk,
    parse_binance_position,
)
from freqtrade.hedge.config import HedgeRuntimeConfig


def runtime_config(
    *,
    adapter: str | None = "binance",
    read_only: bool = True,
) -> HedgeRuntimeConfig:
    return HedgeRuntimeConfig(
        position_mode=PositionMode.HEDGE,
        enabled=True,
        managed_pair="ETH/USDT:USDT",
        exchange_adapter=adapter,
        read_only=read_only,
    )


def account_balance(
    *,
    wallet: str = "1000",
    equity: str = "1050",
    available: str = "800",
    initial_margin: str = "200",
    maintenance_margin: str = "20",
) -> dict:
    return {
        "info": {
            "totalWalletBalance": wallet,
            "totalMarginBalance": equity,
            "availableBalance": available,
            "totalInitialMargin": initial_margin,
            "totalMaintMargin": maintenance_margin,
            "apiKey": "must-not-appear",
        }
    }


def test_parse_binance_account_risk() -> None:
    risk = parse_binance_account_risk(account_balance())

    assert risk.wallet_balance == 1000
    assert risk.equity == 1050
    assert risk.available_balance == 800
    assert risk.initial_margin == 200
    assert risk.maintenance_margin == 20
    assert risk.margin_ratio == Decimal("20") / Decimal("1050")


@pytest.mark.parametrize(
    "balance",
    [
        {},
        account_balance(equity="0"),
        account_balance(available="-1"),
        account_balance(initial_margin="invalid"),
    ],
)
def test_parse_binance_account_risk_fails_closed(balance: dict) -> None:
    with pytest.raises(OperationalException):
        parse_binance_account_risk(balance)


def test_parse_binance_hedge_position() -> None:
    record = parse_binance_position(
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 2,
            "side": "short",
            "entryPrice": 3200,
            "leverage": 5,
            "initialMargin": 1280,
            "info": {"positionSide": "SHORT"},
        }
    )

    assert record is not None
    assert record.position_side is PositionSide.SHORT
    assert record.amount == 2
    assert record.source == "binance"


def test_parse_binance_signed_oneway_position() -> None:
    record = parse_binance_position(
        {
            "symbol": "ETH/USDT:USDT",
            "info": {"positionAmt": "-1.25", "positionSide": "BOTH"},
        }
    )

    assert record is not None
    assert record.position_side is PositionSide.SHORT
    assert record.amount == 1.25


def test_parse_binance_zero_position_is_ignored() -> None:
    assert parse_binance_position({"symbol": "ETH/USDT:USDT", "contracts": 0}) is None


def test_parse_binance_invalid_amount_fails_closed() -> None:
    with pytest.raises(OperationalException, match="invalid amount"):
        parse_binance_position(
            {"symbol": "ETH/USDT:USDT", "contracts": "not-a-number"}
        )


def test_adapter_reads_mode_and_sorted_nonzero_positions() -> None:
    api = Mock()
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": True}
    api.fetch_positions.return_value = [
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 1,
            "info": {"positionSide": "SHORT"},
        },
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 0,
            "info": {"positionSide": "BOTH"},
        },
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 2,
            "info": {"positionSide": "LONG"},
        },
    ]

    snapshot = BinanceReadonlyAdapter(api, runtime_config()).fetch_snapshot(
        "ETH/USDT:USDT"
    )

    assert snapshot.position_mode is PositionMode.HEDGE
    assert [record.position_side for record in snapshot.positions] == [
        PositionSide.LONG,
        PositionSide.SHORT,
    ]
    api.fetch_positions.assert_called_once_with(["ETH/USDT:USDT"], params={})


def test_position_mode_response_fails_closed_when_malformed() -> None:
    api = Mock()
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": "true"}

    with pytest.raises(OperationalException, match="boolean dualSidePosition"):
        BinanceReadonlyAdapter(api, runtime_config()).fetch_position_mode()


@pytest.mark.parametrize("response", [None, [], "hedge"])
def test_position_mode_response_requires_mapping(response: object) -> None:
    api = Mock()
    api.fapiPrivateGetPositionSideDual.return_value = response

    with pytest.raises(OperationalException, match="must be an object"):
        BinanceReadonlyAdapter(api, runtime_config()).fetch_position_mode()


@pytest.mark.parametrize("response", [None, {}, ["invalid"]])
def test_positions_response_requires_list_of_mappings(response: object) -> None:
    api = Mock()
    api.fetch_positions.return_value = response

    with pytest.raises(OperationalException, match="list of objects"):
        BinanceReadonlyAdapter(api, runtime_config()).fetch_positions()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ccxt.DDoSProtection("slow down"), DDosProtection),
        (ccxt.OperationFailed("retry"), TemporaryError),
        (ccxt.AuthenticationError("denied"), OperationalException),
    ],
)
def test_private_api_errors_are_classified(
    error: ccxt.BaseError,
    expected: type[Exception],
) -> None:
    api = Mock()
    api.fapiPrivateGetPositionSideDual.side_effect = error

    with pytest.raises(expected):
        BinanceReadonlyAdapter(api, runtime_config()).fetch_position_mode()


def test_account_check_reconciles_managed_pair() -> None:
    api = Mock()
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": True}
    api.fetch_positions.return_value = [
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 2,
            "info": {"positionSide": "LONG"},
        },
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 1,
            "info": {"positionSide": "SHORT"},
        },
    ]
    trades = [
        SimpleNamespace(
            pair="ETH/USDT:USDT",
            position_side=PositionSide.LONG,
            amount=2,
            is_open=True,
        ),
        SimpleNamespace(
            pair="ETH/USDT:USDT",
            position_side=PositionSide.SHORT,
            amount=1,
            is_open=True,
        ),
    ]

    result = BinanceReadonlyAdapter(api, runtime_config()).check_account(trades)

    assert result.consistent
    assert not result.issues
    assert result.reconciliation.consistent


def test_account_check_report_is_structured_and_excludes_raw_credentials() -> None:
    api = Mock()
    api.apiKey = "must-not-appear"
    api.secret = "must-not-appear"
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": True}
    api.fetch_positions.return_value = [
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 2,
            "info": {
                "positionSide": "LONG",
                "apiKey": "must-not-appear",
            },
        }
    ]
    trade = SimpleNamespace(
        pair="ETH/USDT:USDT",
        position_side=PositionSide.LONG,
        amount=1,
        is_open=True,
    )

    report = BinanceReadonlyAdapter(api, runtime_config()).check_account([trade]).to_dict()
    serialized = str(report)

    assert report["exchange"] == "binance"
    assert report["configured_position_mode"] == "hedge"
    assert report["account_position_mode"] == "hedge"
    assert report["positions"] == [
        {
            "symbol": "ETH/USDT:USDT",
            "position_side": "LONG",
            "amount": "2",
        }
    ]
    assert report["reconciliation_issues"][0]["code"] == "AMOUNT_MISMATCH"
    assert "must-not-appear" not in serialized
    assert "apiKey" not in serialized
    assert "secret" not in serialized


def test_risk_assessment_allows_increase_only_when_consistent() -> None:
    api = Mock()
    api.fetch_balance.return_value = account_balance()
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": True}
    api.fetch_positions.return_value = []

    assessment = BinanceReadonlyAdapter(api, runtime_config()).assess_account([])

    assert assessment.state is BinanceReadonlyState.CONSISTENT
    assert assessment.allows_risk_increase
    assert assessment.halt_reasons == ()
    assert assessment.to_dict()["account"]["equity"] == "1050"
    assert "must-not-appear" not in str(assessment.to_dict())


def test_risk_assessment_halts_on_drift() -> None:
    api = Mock()
    api.fetch_balance.return_value = account_balance()
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": True}
    api.fetch_positions.return_value = [
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 1,
            "info": {"positionSide": "LONG"},
        }
    ]

    assessment = BinanceReadonlyAdapter(api, runtime_config()).assess_account([])

    assert assessment.state is BinanceReadonlyState.HALT
    assert not assessment.allows_risk_increase
    assert assessment.halt_reasons == ("MISSING_LOCAL",)


@pytest.mark.parametrize(
    "balance",
    [
        account_balance(equity="100", available="101"),
        account_balance(
            equity="100",
            available="20",
            initial_margin="90",
            maintenance_margin="20",
        ),
    ],
)
def test_risk_assessment_halts_on_impossible_margin_state(balance: dict) -> None:
    api = Mock()
    api.fetch_balance.return_value = balance
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": True}
    api.fetch_positions.return_value = []

    assessment = BinanceReadonlyAdapter(api, runtime_config()).assess_account([])

    assert assessment.state is BinanceReadonlyState.HALT
    assert not assessment.allows_risk_increase


def test_account_check_reports_mode_and_unmanaged_position() -> None:
    api = Mock()
    api.fapiPrivateGetPositionSideDual.return_value = {"dualSidePosition": False}
    api.fetch_positions.return_value = [
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": 1,
            "info": {"positionSide": "BOTH"},
        }
    ]

    result = BinanceReadonlyAdapter(api, runtime_config()).check_account([])

    assert not result.consistent
    assert [issue.code for issue in result.issues] == [
        "POSITION_MODE_MISMATCH",
        "UNMANAGED_POSITION",
    ]


def test_account_check_requires_managed_pair() -> None:
    config = runtime_config()
    config = HedgeRuntimeConfig(
        position_mode=config.position_mode,
        enabled=config.enabled,
        managed_pair=None,
        exchange_adapter=config.exchange_adapter,
        read_only=config.read_only,
    )

    with pytest.raises(OperationalException, match="requires a managed_pair"):
        BinanceReadonlyAdapter(Mock(), config).check_account([])


@pytest.mark.parametrize(
    ("adapter", "read_only"),
    [(None, True), ("gate", True), ("binance", False)],
)
def test_adapter_rejects_wrong_or_writable_config(
    adapter: str | None,
    read_only: bool,
) -> None:
    with pytest.raises(OperationalException):
        BinanceReadonlyAdapter(
            Mock(),
            runtime_config(adapter=adapter, read_only=read_only),
        )
