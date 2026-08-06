import pytest

from freqtrade.hedge.errors import HedgeSafetyError
from freqtrade.hedge.user_stream_lifecycle import (
    BinanceUserStreamLifecycle,
    UserStreamState,
)


class FakeApi:
    def __init__(self):
        self.keepalives = 0
        self.closed = 0

    def create_listen_key(self):
        return "listen-key"

    def keepalive_listen_key(self, listen_key):
        self.keepalives += 1

    def close_listen_key(self, listen_key):
        self.closed += 1


def test_reconnect_requires_rest_calibration() -> None:
    lifecycle = BinanceUserStreamLifecycle(FakeApi(), keepalive_interval_ms=100)
    lifecycle.start(now_ms=0)
    assert lifecycle.status(now_ms=10, max_age_ms=100).state is UserStreamState.DEGRADED
    lifecycle.mark_rest_calibrated(observed_at_ms=10)
    assert lifecycle.status(now_ms=20, max_age_ms=100).state is UserStreamState.READY
    lifecycle.disconnect()
    status = lifecycle.status(now_ms=30, max_age_ms=100)
    assert status.state is UserStreamState.DEGRADED
    assert status.rest_calibration_required


def test_out_of_order_event_requires_calibration() -> None:
    lifecycle = BinanceUserStreamLifecycle(FakeApi())
    lifecycle.start(now_ms=0)
    lifecycle.mark_rest_calibrated(observed_at_ms=100)
    lifecycle.record_event(event_time_ms=99)
    assert (
        "OUT_OF_ORDER_EVENT"
        in lifecycle.status(
            now_ms=100,
            max_age_ms=100,
        ).reasons
    )


def test_keepalive_failure_is_fail_closed() -> None:
    class Broken(FakeApi):
        def keepalive_listen_key(self, listen_key):
            raise RuntimeError("network")

    lifecycle = BinanceUserStreamLifecycle(Broken(), keepalive_interval_ms=10)
    lifecycle.start(now_ms=0)
    with pytest.raises(HedgeSafetyError):
        lifecycle.tick(now_ms=20)
