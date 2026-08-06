from decimal import Decimal

import pytest

from freqtrade.enums import PositionSide
from freqtrade.exceptions import OperationalException
from freqtrade.hedge.binance_user_stream import (
    BinanceStreamState,
    BinanceUserStreamMirror,
    parse_binance_account_update,
    parse_binance_order_update,
)
from freqtrade.hedge.position_book import PositionRecord


def account_update() -> dict:
    return {
        "e": "ACCOUNT_UPDATE",
        "E": 1564745798939,
        "T": 1564745798938,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": "122624.123", "cw": "100.123", "bc": "0"}],
            "P": [
                {
                    "s": "ETHUSDT",
                    "pa": "1.25",
                    "ep": "3200",
                    "iw": "0",
                    "ps": "LONG",
                },
                {
                    "s": "ETHUSDT",
                    "pa": "-0.5",
                    "ep": "3300",
                    "iw": "0",
                    "ps": "SHORT",
                },
                {
                    "s": "BTCUSDT",
                    "pa": "1",
                    "ep": "100000",
                    "iw": "0",
                    "ps": "LONG",
                },
            ],
        },
    }


def order_update() -> dict:
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": 2000,
        "T": 1999,
        "o": {
            "s": "ETHUSDT",
            "c": "hedge-group-long-open-1",
            "S": "BUY",
            "ps": "LONG",
            "i": 8886774,
            "x": "TRADE",
            "X": "PARTIALLY_FILLED",
            "q": "1",
            "z": "0.4",
            "l": "0.4",
            "ap": "3200",
            "rp": "0",
        },
    }


def test_parse_order_trade_update() -> None:
    update = parse_binance_order_update(order_update(), "ETH/USDT:USDT")

    assert update.client_order_id == "hedge-group-long-open-1"
    assert update.position_side is PositionSide.LONG
    assert update.order_status == "PARTIALLY_FILLED"
    assert update.original_quantity == 1
    assert update.cumulative_filled_quantity == Decimal("0.4")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(e="ACCOUNT_UPDATE"),
        lambda event: event.pop("o"),
        lambda event: event["o"].update(s="BTCUSDT"),
        lambda event: event["o"].update(c=""),
        lambda event: event["o"].update(S="HOLD"),
        lambda event: event["o"].update(ps="BOTH"),
        lambda event: event["o"].update(z="2"),
        lambda event: event["o"].update(l="0.5", z="0.4"),
        lambda event: event["o"].update(ap="-1"),
    ],
)
def test_malformed_order_trade_update_fails_closed(mutate) -> None:
    event = order_update()
    mutate(event)

    with pytest.raises(OperationalException):
        parse_binance_order_update(event, "ETH/USDT:USDT")


def test_parse_account_update_for_managed_pair() -> None:
    update = parse_binance_account_update(account_update(), "ETH/USDT:USDT")

    assert update.reason == "ORDER"
    assert update.event_time_ms == 1564745798939
    assert update.balances[0].asset == "USDT"
    assert [position.position_side for position in update.positions] == [
        PositionSide.LONG,
        PositionSide.SHORT,
    ]
    assert [position.amount for position in update.positions] == [1.25, 0.5]
    assert update.unexpected_symbols == ("BTCUSDT",)


def managed_account_update(*, event_time: int = 2000) -> dict:
    event = account_update()
    event["E"] = event_time
    event["T"] = event_time
    event["a"]["P"] = event["a"]["P"][:2]
    return event


def rest_positions() -> tuple[PositionRecord, ...]:
    return (
        PositionRecord("ETH/USDT:USDT", PositionSide.LONG, "1"),
        PositionRecord("ETH/USDT:USDT", PositionSide.SHORT, "0.5"),
    )


def test_stream_mirror_requires_rest_bootstrap() -> None:
    mirror = BinanceUserStreamMirror("ETH/USDT:USDT")

    status = mirror.status(now_ms=1000, max_age_ms=100)

    assert status.state is BinanceStreamState.HALT
    assert "REST_BOOTSTRAP_REQUIRED" in status.reasons


def test_stream_mirror_merges_partial_updates_and_zero_removes_leg() -> None:
    mirror = BinanceUserStreamMirror("ETH/USDT:USDT")
    mirror.bootstrap(rest_positions(), observed_at_ms=1000)
    event = managed_account_update()
    event["a"]["P"] = [event["a"]["P"][0]]
    event["a"]["P"][0]["pa"] = "2"
    mirror.apply(parse_binance_account_update(event, "ETH/USDT:USDT"))

    first = mirror.status(now_ms=2050, max_age_ms=100)
    assert first.state is BinanceStreamState.READY
    assert [(item.position_side, item.amount) for item in first.positions] == [
        (PositionSide.LONG, 2),
        (PositionSide.SHORT, 0.5),
    ]

    event["E"] = 2100
    event["T"] = 2100
    event["a"]["P"][0]["pa"] = "0"
    mirror.apply(parse_binance_account_update(event, "ETH/USDT:USDT"))

    second = mirror.status(now_ms=2150, max_age_ms=100)
    assert [item.position_side for item in second.positions] == [PositionSide.SHORT]


def test_stream_mirror_halts_on_out_of_order_or_disconnect() -> None:
    mirror = BinanceUserStreamMirror("ETH/USDT:USDT")
    mirror.bootstrap(rest_positions(), observed_at_ms=2000)
    mirror.apply(
        parse_binance_account_update(
            managed_account_update(event_time=1999),
            "ETH/USDT:USDT",
        )
    )
    mirror.disconnect()

    status = mirror.status(now_ms=2050, max_age_ms=100)
    assert status.state is BinanceStreamState.HALT
    assert status.reasons == (
        "OUT_OF_ORDER_EVENT",
        "USER_STREAM_DISCONNECTED",
    )


def test_stream_mirror_halts_when_stale_or_rest_drifted() -> None:
    mirror = BinanceUserStreamMirror("ETH/USDT:USDT")
    mirror.bootstrap(rest_positions(), observed_at_ms=1000)
    mirror.calibrate_rest(
        (PositionRecord("ETH/USDT:USDT", PositionSide.LONG, "3"),)
    )

    status = mirror.status(now_ms=1200, max_age_ms=100)
    assert status.state is BinanceStreamState.HALT
    assert status.reasons == ("REST_POSITION_DRIFT", "STALE_USER_STREAM")


def test_stream_mirror_halts_on_unmanaged_position_event() -> None:
    mirror = BinanceUserStreamMirror("ETH/USDT:USDT")
    mirror.bootstrap(rest_positions(), observed_at_ms=1000)
    mirror.apply(parse_binance_account_update(account_update(), "ETH/USDT:USDT"))

    status = mirror.status(now_ms=1564745798940, max_age_ms=100)
    assert status.state is BinanceStreamState.HALT
    assert status.reasons == ("UNMANAGED_POSITION",)


def test_stream_mirror_tracks_known_partial_fill() -> None:
    mirror = BinanceUserStreamMirror("ETH/USDT:USDT")
    mirror.bootstrap(rest_positions(), observed_at_ms=1000)
    mirror.apply_order(
        parse_binance_order_update(order_update(), "ETH/USDT:USDT"),
        known_client_order_ids={"hedge-group-long-open-1"},
    )

    status = mirror.status(now_ms=2050, max_age_ms=100)
    assert status.state is BinanceStreamState.READY
    assert status.orders[0].order_status == "PARTIALLY_FILLED"
    assert status.orders[0].cumulative_filled_quantity == Decimal("0.4")


def test_stream_mirror_halts_on_unknown_order() -> None:
    mirror = BinanceUserStreamMirror("ETH/USDT:USDT")
    mirror.bootstrap(rest_positions(), observed_at_ms=1000)
    mirror.apply_order(
        parse_binance_order_update(order_update(), "ETH/USDT:USDT"),
        known_client_order_ids=set(),
    )

    status = mirror.status(now_ms=2050, max_age_ms=100)
    assert status.state is BinanceStreamState.HALT
    assert status.reasons == ("UNKNOWN_ORDER",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(e="ORDER_TRADE_UPDATE"),
        lambda event: event.pop("a"),
        lambda event: event["a"].update(B={}),
        lambda event: event["a"].update(P={}),
        lambda event: event.update(E=-1),
        lambda event: event["a"]["P"][0].update(ps="BOTH"),
        lambda event: event["a"]["P"][0].update(pa="invalid"),
    ],
)
def test_malformed_account_update_fails_closed(mutate) -> None:
    event = account_update()
    mutate(event)

    with pytest.raises(OperationalException):
        parse_binance_account_update(event, "ETH/USDT:USDT")
