"""Read-only Binance user-stream lifecycle controller."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from freqtrade.hedge.errors import HedgeSafetyError
from freqtrade.hedge.numeric import require_nonnegative_int


class UserStreamState(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    HALT = "HALT"


class BinanceListenKeyApi(Protocol):
    def create_listen_key(self) -> str: ...
    def keepalive_listen_key(self, listen_key: str) -> None: ...
    def close_listen_key(self, listen_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class UserStreamStatus:
    state: UserStreamState
    listen_key_present: bool
    last_event_time_ms: int | None
    last_keepalive_time_ms: int | None
    rest_calibration_required: bool
    reasons: tuple[str, ...]


class BinanceUserStreamLifecycle:
    def __init__(
        self,
        api: BinanceListenKeyApi,
        *,
        keepalive_interval_ms: int = 30 * 60 * 1000,
    ) -> None:
        self._api = api
        self._keepalive_interval_ms = require_nonnegative_int(
            keepalive_interval_ms,
            field="keepalive_interval_ms",
        )
        if self._keepalive_interval_ms == 0:
            raise ValueError("keepalive_interval_ms must be positive.")
        self._listen_key: str | None = None
        self._state = UserStreamState.STOPPED
        self._last_event_time_ms: int | None = None
        self._last_keepalive_time_ms: int | None = None
        self._rest_calibration_required = True
        self._reasons: list[str] = ["REST_CALIBRATION_REQUIRED"]

    def start(self, *, now_ms: int) -> None:
        self._state = UserStreamState.CONNECTING
        listen_key = self._api.create_listen_key()
        if not isinstance(listen_key, str) or not listen_key.strip():
            self._state = UserStreamState.HALT
            self._add_reason("LISTEN_KEY_CREATION_FAILED")
            raise HedgeSafetyError("Binance listen key is missing.")
        self._listen_key = listen_key.strip()
        self._last_keepalive_time_ms = now_ms
        self._state = UserStreamState.DEGRADED
        self._rest_calibration_required = True
        self._add_reason("REST_CALIBRATION_REQUIRED")

    def mark_rest_calibrated(self, *, observed_at_ms: int) -> None:
        if self._listen_key is None:
            raise HedgeSafetyError("Cannot calibrate before user stream start.")
        self._last_event_time_ms = observed_at_ms
        self._rest_calibration_required = False
        for reason in (
            "REST_CALIBRATION_REQUIRED",
            "USER_STREAM_DISCONNECTED",
            "OUT_OF_ORDER_EVENT",
            "LISTEN_KEY_KEEPALIVE_FAILED",
        ):
            self._remove_reason(reason)
        self._state = UserStreamState.READY if not self._reasons else UserStreamState.DEGRADED

    def record_event(self, *, event_time_ms: int) -> None:
        if self._listen_key is None:
            self._state = UserStreamState.HALT
            self._add_reason("USER_STREAM_NOT_STARTED")
            return
        if self._last_event_time_ms is not None and event_time_ms < self._last_event_time_ms:
            self._state = UserStreamState.DEGRADED
            self._rest_calibration_required = True
            self._add_reason("OUT_OF_ORDER_EVENT")
            self._add_reason("REST_CALIBRATION_REQUIRED")
            return
        self._last_event_time_ms = event_time_ms

    def tick(self, *, now_ms: int) -> None:
        if self._listen_key is None:
            return
        if (
            self._last_keepalive_time_ms is None
            or now_ms - self._last_keepalive_time_ms >= self._keepalive_interval_ms
        ):
            try:
                self._api.keepalive_listen_key(self._listen_key)
            except Exception as exc:
                self._state = UserStreamState.DEGRADED
                self._rest_calibration_required = True
                self._add_reason("LISTEN_KEY_KEEPALIVE_FAILED")
                self._add_reason("REST_CALIBRATION_REQUIRED")
                raise HedgeSafetyError("Listen-key keepalive failed.") from exc
            self._last_keepalive_time_ms = now_ms

    def disconnect(self) -> None:
        self._state = UserStreamState.DEGRADED
        self._rest_calibration_required = True
        self._add_reason("USER_STREAM_DISCONNECTED")
        self._add_reason("REST_CALIBRATION_REQUIRED")

    def stop(self) -> None:
        if self._listen_key is not None:
            self._api.close_listen_key(self._listen_key)
        self._listen_key = None
        self._state = UserStreamState.STOPPED
        self._rest_calibration_required = True
        self._reasons = ["REST_CALIBRATION_REQUIRED"]

    def status(self, *, now_ms: int, max_age_ms: int) -> UserStreamStatus:
        max_age = require_nonnegative_int(max_age_ms, field="max_age_ms")
        if max_age == 0:
            raise ValueError("max_age_ms must be positive.")
        reasons = list(self._reasons)
        if self._state is not UserStreamState.STOPPED and (
            self._last_event_time_ms is None or now_ms - self._last_event_time_ms > max_age
        ):
            reasons.append("STALE_USER_STREAM")
        reasons = list(dict.fromkeys(reasons))
        if self._state is UserStreamState.HALT:
            state = UserStreamState.HALT
        elif reasons and self._state is not UserStreamState.STOPPED:
            state = UserStreamState.DEGRADED
        else:
            state = self._state
        return UserStreamStatus(
            state=state,
            listen_key_present=self._listen_key is not None,
            last_event_time_ms=self._last_event_time_ms,
            last_keepalive_time_ms=self._last_keepalive_time_ms,
            rest_calibration_required=self._rest_calibration_required,
            reasons=tuple(reasons),
        )

    def _add_reason(self, reason: str) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)

    def _remove_reason(self, reason: str) -> None:
        if reason in self._reasons:
            self._reasons.remove(reason)
