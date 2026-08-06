"""Synchronous outbox publisher adapters for telemetry and tests."""

from __future__ import annotations

import asyncio
from threading import RLock
from typing import Callable

from freqtrade.hedge.contracts.events import OutboxEvent
from freqtrade.hedge.telemetry.events import HedgeEventType, HedgeTelemetryEvent


class InMemoryEventPublisher:
    def __init__(self, callback: Callable[[OutboxEvent], None] | None = None) -> None:
        self._events: list[OutboxEvent] = []
        self._callback = callback
        self._lock = RLock()

    def publish(self, event: OutboxEvent) -> None:
        if not isinstance(event, OutboxEvent):
            raise TypeError("event must be an OutboxEvent")
        with self._lock:
            self._events.append(event)
        if self._callback is not None:
            self._callback(event)

    def events(self) -> tuple[OutboxEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def set_callback(self, callback: Callable[[OutboxEvent], None] | None) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("callback must be callable or None")
        with self._lock:
            self._callback = callback


class HedgeEventHubPublisher:
    """Publish execution outbox events to an async HedgeEventHub from sync code."""

    def __init__(self, hub: object) -> None:
        publish = getattr(hub, "publish", None)
        if not callable(publish):
            raise TypeError("hub must expose async publish(event)")
        self._hub = hub

    def publish(self, event: OutboxEvent) -> None:
        category = event.event_type.split("_", 1)[0]
        event_type = HedgeEventType.ORDER
        if category == "INTENT":
            event_type = HedgeEventType.INTENT
        elif category == "FILL":
            event_type = HedgeEventType.FILL
        elif category == "HALT":
            event_type = HedgeEventType.HALT
        telemetry = HedgeTelemetryEvent(
            event_type=event_type,
            payload={"event_type": event.event_type, **dict(event.payload)},
            account_id=str(event.payload.get("account_id", "default")),
            symbol=event.payload.get("symbol"),
            correlation_id=event.correlation_id,
            occurred_at=event.occurred_at,
        )
        coroutine = self._hub.publish(telemetry)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coroutine)
        else:
            loop.create_task(coroutine)
