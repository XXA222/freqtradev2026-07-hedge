from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from freqtrade.hedge.exchange.base import (
    AccountConfigurationFact,
    AccountSnapshotFact,
    BalanceFact,
    PositionFact,
)
from freqtrade.hedge.exchange.binance_readonly import BinanceAccountBundle
from freqtrade.hedge.exchange.binance_user_stream import BinanceUserStream
from freqtrade.hedge.exchange.listen_key import ListenKeyManager
from freqtrade.hedge.readonly.calibration import ReadonlyCalibration
from freqtrade.hedge.readonly.freshness import FreshnessPolicy, UserStreamFreshness
from freqtrade.hedge.readonly.runtime import BinanceReadonlyRuntime, BinanceReadonlyRuntimeConfig
from freqtrade.hedge.readonly.scheduler import CalibrationSchedule, ReconciliationScheduler
from freqtrade.hedge.readonly.service import BinanceReadonlyService
from r25_toolkit.sqlite_repository import DurableReadonlyRepository


class RealtimeClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class Transport:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class Client:
    def __init__(self, clock: RealtimeClock) -> None:
        self.account_id = "binance-usdm:test-runtime"
        self.clock = clock
        self.transport = Transport()
        self.positions = (self.position("0", 1000),)
        self.bundle_calls = 0

    def position(self, quantity: str, update_time_ms: int) -> PositionFact:
        return PositionFact(
            account_id=self.account_id,
            symbol="BTCUSDT",
            position_side="LONG",
            quantity=Decimal(quantity),
            entry_price=Decimal("100"),
            mark_price=Decimal("101"),
            unrealized_pnl=Decimal("0"),
            liquidation_price=None,
            leverage=5,
            margin_mode="cross",
            update_time_ms=update_time_ms,
            observed_at=self.clock.now(),
            source="BINANCE_REST",
        )

    async def synchronize_clock(self) -> None:
        return None

    async def preflight_permissions(self, policy: object) -> None:
        del policy

    async def fetch_bundle(
        self, *, include_fills: bool, fill_start_time_ms: int | None = None
    ) -> BinanceAccountBundle:
        del include_fills, fill_start_time_ms
        self.bundle_calls += 1
        now = self.clock.now()
        return BinanceAccountBundle(
            account_snapshot=AccountSnapshotFact(
                account_id=self.account_id,
                total_wallet_balance=Decimal("1000"),
                total_available_balance=Decimal("900"),
                total_margin_balance=Decimal("1000"),
                total_initial_margin=Decimal("0"),
                total_maintenance_margin=Decimal("0"),
                total_unrealized_pnl=Decimal("0"),
                observed_at=now,
                collection_started_at=now,
                collection_completed_at=now,
            ),
            balances=(
                BalanceFact(
                    account_id=self.account_id,
                    asset="USDT",
                    wallet_balance=Decimal("1000"),
                    available_balance=Decimal("900"),
                    cross_wallet_balance=Decimal("1000"),
                    unrealized_pnl=Decimal("0"),
                    observed_at=now,
                    source="BINANCE_REST",
                ),
            ),
            positions=self.positions,
            open_orders=(),
            fills=(),
            configuration=AccountConfigurationFact(
                account_id=self.account_id,
                hedge_mode=True,
                active_margin_modes=("cross",),
                leverage_by_symbol_side={
                    "BTCUSDT:LONG": 5,
                    "BTCUSDT:SHORT": 5,
                },
                observed_at=now,
            ),
            income_events=(),
            collection_started_at=now,
            collection_completed_at=now,
        )


class ListenKeyClient:
    def __init__(self) -> None:
        self.created = 0
        self.closed = 0

    async def create(self) -> str:
        self.created += 1
        return f"key-{self.created}"

    async def keepalive(self, key: str | None = None) -> None:
        del key

    async def close(self, key: str | None = None) -> None:
        del key
        self.closed += 1


class Connection:
    def __init__(self, initial_messages: tuple[dict[str, object], ...] = ()) -> None:
        self.queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self.closed = False
        for message in initial_messages:
            self.queue.put_nowait(message)

    def __aiter__(self):
        return self.messages()

    async def messages(self):
        while True:
            message = await self.queue.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)


class Connector:
    def __init__(self, first_message: dict[str, object]) -> None:
        self.first_message = first_message
        self.connections: list[Connection] = []

    async def connect(self, url: str) -> Connection:
        assert url.endswith("/key-1")
        messages = (self.first_message,) if not self.connections else ()
        connection = Connection(messages)
        self.connections.append(connection)
        return connection


def account_update() -> dict[str, object]:
    return {
        "e": "ACCOUNT_UPDATE",
        "E": 2000,
        "T": 2000,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": "1000", "cw": "1000", "bc": "0"}],
            "P": [
                {
                    "s": "BTCUSDT",
                    "ps": "LONG",
                    "pa": "1",
                    "ep": "100",
                    "up": "1",
                    "mt": "cross",
                }
            ],
        },
    }


async def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.005)


def test_durable_repository_runs_real_stream_components_and_reconnects(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = RealtimeClock()
        repository = DurableReadonlyRepository(tmp_path / "runtime.sqlite")
        client = Client(clock)
        listen_key_client = ListenKeyClient()
        listen_keys = ListenKeyManager(
            client=listen_key_client,
            clock=clock,
            ttl=timedelta(minutes=60),
            renew_interval=timedelta(minutes=30),
            error_retry_interval=timedelta(seconds=1),
        )
        connector = Connector(account_update())
        stream = BinanceUserStream(
            account_id=client.account_id,
            managed_symbols=("BTCUSDT",),
            repository=repository,
            listen_keys=listen_keys,
            connector=connector,
            websocket_base_url="ws://localhost:18082/ws",
            clock=clock,
            min_reconnect_delay_seconds=0.01,
            max_reconnect_delay_seconds=0.02,
            reconnect_reset_after_seconds=0.1,
        )
        calibration = ReadonlyCalibration(
            client=client,
            repository=repository,
            managed_symbols=("BTCUSDT",),
            clock=clock,
        )
        scheduler = ReconciliationScheduler(
            calibration=calibration,
            schedule=CalibrationSchedule(
                fast_interval=timedelta(hours=1),
                full_interval=timedelta(hours=2),
                error_retry_interval=timedelta(seconds=1),
            ),
            clock=clock,
        )
        service = BinanceReadonlyService(
            client=client,
            calibration=calibration,
            stream=stream,
            scheduler=scheduler,
            freshness=UserStreamFreshness(
                FreshnessPolicy(
                    event_stale_after=None,
                    calibration_stale_after=timedelta(hours=3),
                )
            ),
            clock=clock,
        )
        runtime = BinanceReadonlyRuntime(
            config=BinanceReadonlyRuntimeConfig(
                account_id=client.account_id,
                managed_symbols=("BTCUSDT",),
                api_key="test-key-value",
                api_secret="test-secret-value",
            ),
            transport=client.transport,
            client=client,
            listen_keys=listen_keys,
            stream=stream,
            calibration=calibration,
            scheduler=scheduler,
            service=service,
            clock=clock,
        )
        await runtime.start()
        try:
            await runtime.wait_until_ready(timeout_seconds=1)
            async def position_event_applied() -> bool:
                return any(
                    item.quantity == 1
                    for item in await repository.load_active_positions(client.account_id)
                )

            await wait_for(position_event_applied)
            client.positions = (client.position("1", 2000),)
            previous_calls = client.bundle_calls
            await runtime.request_reconnect()
            await wait_for(lambda: client.bundle_calls > previous_calls)
            await runtime.wait_until_ready(timeout_seconds=1)
            assert len(connector.connections) >= 2
        finally:
            await runtime.stop()
        repository.close()

        reopened = DurableReadonlyRepository(tmp_path / "runtime.sqlite")
        active = await reopened.load_active_positions(client.account_id)
        assert any(item.quantity == 1 for item in active)
        assert reopened.event_source_counts().get("BINANCE_USER_STREAM", 0) >= 1
        assert reopened.integrity()["status"] == "PASS"
        reopened.close()

    # Avoid pytest-asyncio and the upstream test conftest dependency.
    asyncio.run(scenario())
