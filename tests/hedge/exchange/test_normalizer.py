from datetime import UTC, datetime
from decimal import Decimal

import pytest

from freqtrade.hedge.exchange.binance_normalizer import (
    finite_decimal,
    normalize_account_snapshot,
    normalize_configuration,
    normalize_fill,
    normalize_order,
    normalize_position,
    normalize_positions,
    observed_time,
)
from freqtrade.hedge.exchange.rate_limit import BinanceDataError


def test_finite_decimal_rejects_nan_and_infinity():
    for value in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(BinanceDataError):
            finite_decimal(value, field="x")


def test_explicit_position_side_must_match_signed_amount():
    payload = {
        "symbol": "BTCUSDT",
        "positionSide": "LONG",
        "positionAmt": "-1",
        "marginType": "cross",
    }
    with pytest.raises(BinanceDataError):
        normalize_position(payload, account_id="acct")

    payload.update(positionSide="SHORT", positionAmt="-2")
    fact = normalize_position(payload, account_id="acct")
    assert fact.quantity == Decimal("2")


def test_string_false_is_not_treated_as_true_for_reduce_only_and_buyer():
    order = normalize_order(
        {
            "symbol": "BTCUSDT",
            "positionSide": "LONG",
            "orderId": 1,
            "side": "BUY",
            "type": "LIMIT",
            "status": "NEW",
            "origQty": "1",
            "executedQty": "0",
            "avgPrice": "0",
            "reduceOnly": "false",
        },
        account_id="acct",
    )
    assert order.reduce_only is False

    fill = normalize_fill(
        {
            "symbol": "BTCUSDT",
            "positionSide": "LONG",
            "id": 1,
            "orderId": 1,
            "buyer": "false",
            "qty": "1",
            "price": "1",
        },
        account_id="acct",
    )
    assert fill.side == "SELL"


def test_configuration_boolean_is_strict():
    with pytest.raises(BinanceDataError):
        normalize_configuration(
            account_id="acct",
            dual_side_payload={"dualSidePosition": "yes"},
            symbol_configurations=[],
            managed_symbols=["BTCUSDT"],
        )


def test_duplicate_position_and_asset_rows_are_rejected():
    row = {
        "symbol": "BTCUSDT",
        "positionSide": "LONG",
        "positionAmt": "0",
        "marginType": "cross",
        "leverage": "5",
    }
    with pytest.raises(BinanceDataError, match="duplicate position"):
        normalize_positions([row, row], account_id="acct")

    now = datetime.now(UTC)
    account_payload = {
        "assets": [
            {"asset": "USDT"},
            {"asset": "USDT"},
        ]
    }
    with pytest.raises(BinanceDataError, match="duplicate account asset"):
        normalize_account_snapshot(
            account_payload,
            account_id="acct",
            collection_started_at=now,
            collection_completed_at=now,
        )


def test_position_requires_explicit_margin_mode_source():
    payload = {
        "symbol": "BTCUSDT",
        "positionSide": "LONG",
        "positionAmt": "1",
    }
    with pytest.raises(BinanceDataError, match="Margin mode"):
        normalize_position(payload, account_id="acct")


def test_observed_time_rejects_naive_datetime():
    with pytest.raises(BinanceDataError, match="explicit timezone"):
        observed_time(datetime(2026, 7, 26, 12, 0, 0))
from freqtrade.hedge.exchange.base import stable_fingerprint, to_primitive


def test_stable_fingerprint_is_deterministic_for_sets():
    left = {"values": {"beta", "alpha", "gamma"}}
    right = {"values": {"gamma", "beta", "alpha"}}

    assert to_primitive(left) == {"values": ["alpha", "beta", "gamma"]}
    assert stable_fingerprint(left) == stable_fingerprint(right)


def test_stable_fingerprint_preserves_sequence_order():
    assert stable_fingerprint(["alpha", "beta"]) != stable_fingerprint(
        ["beta", "alpha"]
    )
