from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from freqtrade.hedge.exchange.base import (
    AccountEventFact,
    AccountSnapshotFact,
    BalanceFact,
    CalibrationKind,
    ExchangeFactBatch,
    FillFact,
    OrderFact,
    PositionFact,
    ReconciliationDiffFact,
)
from freqtrade.hedge.exchange.binance_readonly import PermissionPolicy
from r25_toolkit.cli import _validate_r24_report
from r25_toolkit.sqlite_repository import DurableReadonlyRepository


NOW = datetime(2026, 8, 2, 13, 30, tzinfo=UTC)


def _position(quantity: str = "0.01", update_time_ms: int = 1000) -> PositionFact:
    return PositionFact(
        account_id="binance-usdm:test",
        symbol="BTCUSDT",
        position_side="LONG",
        quantity=Decimal(quantity),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60100"),
        unrealized_pnl=Decimal("1"),
        liquidation_price=Decimal("10000"),
        leverage=3,
        margin_mode="cross",
        update_time_ms=update_time_ms,
        observed_at=NOW,
        source="BINANCE_REST",
    )


def _order(status: str = "NEW", update_time_ms: int = 1000) -> OrderFact:
    return OrderFact(
        account_id="binance-usdm:test",
        symbol="BTCUSDT",
        position_side="LONG",
        exchange_order_id="123",
        client_order_id="external-order",
        side="BUY",
        order_type="LIMIT",
        status=status,
        original_quantity=Decimal("0.01"),
        cumulative_filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        reduce_only=False,
        update_time_ms=update_time_ms,
        observed_at=NOW,
        source="BINANCE_REST",
    )


def _fill() -> FillFact:
    return FillFact(
        account_id="binance-usdm:test",
        symbol="BTCUSDT",
        position_side="LONG",
        exchange_trade_id="456",
        exchange_order_id="123",
        side="BUY",
        quantity=Decimal("0.001"),
        price=Decimal("60000"),
        commission=Decimal("0.01"),
        commission_asset="USDT",
        realized_pnl=Decimal("0"),
        event_time_ms=1001,
        observed_at=NOW,
        source="BINANCE_USER_STREAM",
    )


def _balance() -> BalanceFact:
    return BalanceFact(
        account_id="binance-usdm:test",
        asset="USDT",
        wallet_balance=Decimal("1000"),
        available_balance=Decimal("900"),
        cross_wallet_balance=Decimal("1000"),
        unrealized_pnl=Decimal("1"),
        observed_at=NOW,
        source="BINANCE_REST",
    )


def _snapshot() -> AccountSnapshotFact:
    return AccountSnapshotFact(
        account_id="binance-usdm:test",
        total_wallet_balance=Decimal("1000"),
        total_available_balance=Decimal("900"),
        total_margin_balance=Decimal("1001"),
        total_initial_margin=Decimal("100"),
        total_maintenance_margin=Decimal("5"),
        total_unrealized_pnl=Decimal("1"),
        observed_at=NOW,
        collection_started_at=NOW,
        collection_completed_at=NOW,
    )


def _event() -> AccountEventFact:
    return AccountEventFact(
        account_id="binance-usdm:test",
        event_type="ORDER_TRADE_UPDATE",
        event_key="event-1",
        event_time_ms=1001,
        transaction_time_ms=1001,
        payload={"e": "ORDER_TRADE_UPDATE"},
        observed_at=NOW,
        source="BINANCE_USER_STREAM",
    )


def test_durable_repository_is_idempotent_and_reopens(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "runtime.sqlite"
        repository = DurableReadonlyRepository(database)
        run_id = await repository.begin_reconciliation(
            account_id="binance-usdm:test",
            kind=CalibrationKind.STARTUP,
            started_at=NOW,
        )
        diff = ReconciliationDiffFact(
            account_id="binance-usdm:test",
            entity_type="ORDER",
            entity_key="BTCUSDT:123",
            reason_code="EXTERNAL_ORDER",
            expected={"origin": "SYSTEM"},
            observed={"origin": "EXTERNAL"},
            severity="WARNING",
        )
        batch = ExchangeFactBatch(
            account_id="binance-usdm:test",
            source="BINANCE_USER_STREAM",
            observed_at=NOW,
            reconciliation_run_id=run_id,
            account_snapshot=_snapshot(),
            balances=(_balance(),),
            positions=(_position(),),
            orders=(_order(),),
            fills=(_fill(),),
            account_events=(_event(),),
            reconciliation_diffs=(diff,),
            correlation_id="event-1",
        )
        await repository.append_exchange_fact_batch(batch)
        await repository.append_exchange_fact_batch(batch)
        await repository.save_history_cursor(
            "binance-usdm:test", "BINANCE_FUTURES_HISTORY_MS", 123456
        )
        await repository.complete_reconciliation(
            run_id,
            completed_at=NOW,
            status="CONSISTENT",
            reason="CONSISTENT",
        )
        repository.start_run("r25-test", NOW, {"status": "RUNNING"})
        repository.record_observation("r25-test", 0, NOW, {"state": "READY"})
        repository.complete_run(
            "r25-test", completed_at=NOW, status="FULL_PASS", payload={"ok": True}
        )
        counts = repository.counts()
        assert counts["r25_fills"] == 1
        assert counts["r25_account_events"] == 1
        assert counts["r25_reconciliation_diffs"] == 1
        assert counts["r25_fact_events"] == 6
        assert repository.integrity()["status"] == "PASS"
        repository.close()

        reopened = DurableReadonlyRepository(database)
        assert len(await reopened.load_active_positions("binance-usdm:test")) == 1
        assert len(await reopened.load_active_orders("binance-usdm:test")) == 1
        assert await reopened.has_fill("binance-usdm:test", "BTCUSDT", "456")
        assert (
            await reopened.load_history_cursor(
                "binance-usdm:test", "BINANCE_FUTURES_HISTORY_MS"
            )
            == 123456
        )
        assert reopened.counts() == counts
        assert reopened.integrity()["status"] == "PASS"
        await reopened.append_position_snapshots((_position("0", 2000),))
        assert not await reopened.load_active_positions("binance-usdm:test")
        reopened.close()

    asyncio.run(scenario())


def test_r24_report_validation_accepts_readonly_full_pass(tmp_path: Path) -> None:
    report = {
        "status": "FULL_PASS",
        "account_fingerprint": "abc123",
        "run_id": "r24-test",
        "scope": {"symbols": ["BTCUSDT", "ETHUSDT"]},
        "security": {"exchange_writes": "LOCKED"},
        "gate_domains": {
            "clock_readiness": "PASS",
            "authenticated_read_access": "PASS",
            "account_and_hedge_semantics": "PASS",
            "database_persistence": "PASS",
            "history_completeness": "PASS",
            "money_movement_safety": "PASS",
            "strict_key_policy": "NOT_REQUESTED",
        },
    }
    path = tmp_path / "r24.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert _validate_r24_report(path)["account_fingerprint"] == "abc123"


@pytest.mark.parametrize("status", ["FAIL", "", "NOT_RUN"])
def test_r24_report_validation_rejects_failed_prerequisite(
    tmp_path: Path, status: str
) -> None:
    path = tmp_path / "r24.json"
    path.write_text(json.dumps({"status": status}), encoding="utf-8")
    with pytest.raises(ValueError):
        _validate_r24_report(path)


def test_readonly_user_stream_policy_does_not_require_trading_permission() -> None:
    policy = PermissionPolicy(require_futures_enabled=False)
    assert policy.require_futures_enabled is False
    assert policy.forbid_futures_trading_permission is False


def test_cli_observation_builds_pass_report_without_persisting_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argparse import Namespace
    from types import SimpleNamespace
    import time

    import r25_toolkit.cli as cli_module
    from freqtrade.hedge.exchange.base import (
        CalibrationResult,
        Direction2HealthFact,
        ReadonlyAccountView,
        ReadonlyState,
        StreamHealth,
        TransportTelemetry,
    )
    from freqtrade.hedge.readonly.freshness import FreshnessAssessment
    from freqtrade.hedge.readonly.service import ReadonlyRuntimeSnapshot, ServiceStatus

    r24 = {
        "status": "FULL_PASS",
        "account_fingerprint": "fakefingerprint",
        "run_id": "r24-test",
        "scope": {"symbols": ["BTCUSDT"]},
        "security": {"exchange_writes": "LOCKED"},
        "gate_domains": {
            "clock_readiness": "PASS",
            "authenticated_read_access": "PASS",
            "account_and_hedge_semantics": "PASS",
            "database_persistence": "PASS",
            "history_completeness": "PASS",
            "money_movement_safety": "PASS",
        },
    }
    r24_path = tmp_path / "r24.json"
    r24_path.write_text(json.dumps(r24), encoding="utf-8")
    monkeypatch.setenv("BINANCE_API_KEY", "K" * 32)
    monkeypatch.setenv("BINANCE_API_SECRET", "S" * 32)
    monkeypatch.setenv("FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK", "1")

    class Clock:
        def now(self):
            return datetime.now(UTC)

        def monotonic(self):
            return time.monotonic()

        async def sleep(self, seconds):
            await asyncio.sleep(seconds)

    class FakeRuntime:
        def __init__(self, repository):
            self.repository = repository
            self.clock = Clock()
            self.reconnect_count = 0
            self.reconnect_calibration = None

        def _calibration(self, kind=CalibrationKind.FAST):
            now = datetime.now(UTC)
            return CalibrationResult(
                run_id="fake-calibration",
                kind=kind,
                started_at=now,
                completed_at=now,
                position_count=0,
                active_order_count=0,
                fill_count=0,
                diff_count=0,
                unmanaged_positions=(),
                unmanaged_orders=(),
                consistent=True,
                reason="CONSISTENT",
            )

        def snapshot(self):
            now = datetime.now(UTC)
            health = StreamHealth(
                connected=True,
                last_connected_at=now,
                last_event_at=now,
                last_calibration_at=now,
                reconnect_count=self.reconnect_count,
                duplicate_count=0,
                out_of_order_count=0,
                gap_count=0,
            )
            return ReadonlyRuntimeSnapshot(
                status=ServiceStatus(ReadonlyState.READY, "TEST_READY", now, "fake"),
                stream_health=health,
                freshness=FreshnessAssessment(
                    ReadonlyState.READY, True, "FRESH", 0.0, 0.0
                ),
                last_fast_calibration=self._calibration(),
                last_full_calibration=None,
                last_reconnect_calibration=self.reconnect_calibration,
                direction2_health=Direction2HealthFact(
                    account_id="binance-usdm:fakefingerprint",
                    rest_fresh=True,
                    stream_connected=True,
                    stream_fresh=True,
                    clock_synchronized=True,
                    configuration_valid=True,
                    reconciliation_consistent=True,
                    unmanaged_position_count=0,
                    unmanaged_order_count=0,
                    external_order_count=0,
                    reason_codes=(),
                    observed_at=now,
                ),
                transport_telemetry=TransportTelemetry(
                    logical_request_count=5,
                    attempt_count=5,
                    retry_count=0,
                    error_count=0,
                    rate_limit_count=0,
                    server_error_count=0,
                    used_weight=5,
                    last_latency_ms=1.0,
                    last_status=200,
                    last_error=None,
                    last_response_at=now,
                ),
                account_view=ReadonlyAccountView(
                    account_id="binance-usdm:fakefingerprint",
                    observed_at=now,
                    account_snapshot=None,
                    balances=(),
                    positions=(),
                    active_orders=(),
                    configuration=None,
                    revision=1,
                    last_calibration_run_id="fake",
                ),
                listen_key_generation=1,
                listen_key_created_at=now,
                listen_key_renewed_at=now,
                listen_key_expires_at=now + timedelta(hours=1),
            )

        async def start(self):
            await self.repository.append_account_events(
                (
                    AccountEventFact(
                        account_id="binance-usdm:fakefingerprint",
                        event_type="ACCOUNT_UPDATE",
                        event_key="fake-user-event",
                        event_time_ms=1,
                        transaction_time_ms=1,
                        payload={"e": "ACCOUNT_UPDATE"},
                        observed_at=datetime.now(UTC),
                        source="BINANCE_USER_STREAM",
                    ),
                )
            )

        async def wait_until_ready(self, timeout_seconds):
            del timeout_seconds
            return self.snapshot()

        async def request_reconnect(self):
            self.reconnect_count += 1
            self.reconnect_calibration = self._calibration(CalibrationKind.RECONNECT)

        async def calibrate_now(self, kind):
            return self._calibration(kind)

        async def stop(self):
            return None

    async def fake_scope_discovery(**kwargs):
        initial = tuple(kwargs["initial_symbols"])
        return {
            "initial_symbols": initial,
            "runtime_symbols": initial,
            "auto_observed_symbols": (),
            "active_position_symbols": initial,
            "active_order_symbols": (),
            "position_risk_compat": {"installed": True},
        }

    monkeypatch.setattr(
        cli_module,
        "discover_active_observation_symbols",
        fake_scope_discovery,
    )
    monkeypatch.setattr(
        cli_module,
        "build_binance_readonly_runtime",
        lambda *, config, repository: FakeRuntime(repository),
    )
    args = Namespace(
        r24_report=r24_path,
        proxy="http://127.0.0.1:7897",
        credential_config=None,
        database=tmp_path / "runtime.sqlite",
        observations=tmp_path / "observations.jsonl",
        report=tmp_path / "report.json",
        duration_seconds=0.10,
        observation_interval_seconds=0.01,
        ready_timeout_seconds=1.0,
        fast_interval_seconds=1.0,
        full_interval_seconds=1.0,
        error_retry_seconds=1.0,
        request_timeout_seconds=1.0,
        max_collection_span_seconds=1.0,
        max_clock_skew_ms=1000.0,
        fill_lookback_hours=1.0,
        history_overlap_minutes=0.0,
        max_history_backfill_days=1.0,
        exercise_reconnect=True,
        require_user_event=True,
        log_level="INFO",
    )
    report = asyncio.run(cli_module._observe(args))
    assert report["status"] == "FULL_PASS", report
    assert report["result"]["forced_reconnect_recovered"] is True
    assert report["database"]["reopen_readback"] == "PASS"
    assert report["security"]["artifact_secret_scan"]["status"] == "PASS"
    combined = (tmp_path / "report.json").read_bytes() + (
        tmp_path / "observations.jsonl"
    ).read_bytes() + (tmp_path / "runtime.sqlite").read_bytes()
    assert ("K" * 32).encode() not in combined
    assert ("S" * 32).encode() not in combined



def test_position_v3_missing_margin_mode_is_enriched_from_symbol_config() -> None:
    from freqtrade.hedge.exchange.base import BinanceHttpResponse
    from freqtrade.hedge.exchange.binance_readonly import BinanceReadonlyClient
    from r25_toolkit.position_risk_compat import install_position_risk_compat

    class Clock:
        def now(self) -> datetime:
            return NOW

        def monotonic(self) -> float:
            return 0.0

        async def sleep(self, seconds: float) -> None:
            del seconds

    class Transport:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def set_timestamp_provider(self, provider: object) -> None:
            del provider

        async def request(
            self,
            method: str,
            path: str,
            *,
            params: dict[str, object] | None = None,
            signed: bool = False,
            weight: int = 1,
            **kwargs: object,
        ) -> BinanceHttpResponse:
            del method, params, signed, weight, kwargs
            self.paths.append(path)
            if path == "/fapi/v1/symbolConfig":
                return BinanceHttpResponse(
                    data=[
                        {
                            "symbol": "ETHUSDT",
                            "marginType": "CROSSED",
                            "leverage": 5,
                        }
                    ],
                    status=200,
                )
            if path == "/fapi/v3/positionRisk":
                return BinanceHttpResponse(
                    data=[
                        {
                            "symbol": "ETHUSDT",
                            "positionSide": "LONG",
                            "positionAmt": "0.01",
                            "entryPrice": "3000",
                            "markPrice": "3010",
                            "unRealizedProfit": "0.1",
                            "liquidationPrice": "0",
                            "updateTime": 1000,
                        }
                    ],
                    status=200,
                )
            raise AssertionError(path)

    async def scenario() -> None:
        transport = Transport()
        client = BinanceReadonlyClient(
            transport=transport,
            account_id="binance-usdm:test",
            managed_symbols=("ETHUSDT",),
            clock=Clock(),
        )
        result = install_position_risk_compat(client)
        assert result["installed"] is True
        positions = await client.fetch_positions(None)
        assert len(positions) == 1
        assert positions[0].margin_mode == "cross"
        assert positions[0].leverage == 5
        assert transport.paths == [
            "/fapi/v1/symbolConfig",
            "/fapi/v3/positionRisk",
        ]

    asyncio.run(scenario())



def test_delivery_symbol_compat_accepts_official_date_suffix() -> None:
    from freqtrade.hedge.exchange.binance_normalizer import normalize_position
    from freqtrade.hedge.exchange.symbol_codec import to_binance_symbol
    from r25_toolkit.scope_compat import install_usdm_delivery_symbol_compat

    result = install_usdm_delivery_symbol_compat()
    assert result["installed"] is True
    assert to_binance_symbol("ETHUSDT_261225") == "ETHUSDT_261225"
    position = normalize_position(
        {
            "symbol": "ETHUSDT_261225",
            "positionSide": "LONG",
            "positionAmt": "0.01",
            "entryPrice": "3000",
            "markPrice": "3010",
            "unRealizedProfit": "0.1",
            "liquidationPrice": "0",
            "leverage": 5,
            "marginType": "cross",
            "updateTime": 1000,
        },
        account_id="binance-usdm:test",
        observed_at=NOW,
    )
    assert position.symbol == "ETHUSDT_261225"


def test_active_delivery_contract_is_added_to_readonly_observation_scope() -> None:
    from r25_toolkit.scope_compat import active_observation_symbols

    delivery_position = PositionFact(
        account_id="binance-usdm:test",
        symbol="ETHUSDT_261225",
        position_side="LONG",
        quantity=Decimal("0.01"),
        entry_price=Decimal("3000"),
        mark_price=Decimal("3010"),
        unrealized_pnl=Decimal("0.1"),
        liquidation_price=None,
        leverage=5,
        margin_mode="cross",
        update_time_ms=1000,
        observed_at=NOW,
        source="BINANCE_REST",
    )
    assert active_observation_symbols(
        ("BTCUSDT", "ETHUSDT"),
        (delivery_position,),
        (),
    ) == ("BTCUSDT", "ETHUSDT", "ETHUSDT_261225")


def test_unmanaged_non_ascii_symbol_config_is_filtered_before_strict_normalization() -> None:
    from freqtrade.hedge.exchange.binance_normalizer import normalize_configuration
    from r25_toolkit.managed_symbol_config_compat import (
        install_managed_symbol_config_scope_compat,
    )

    result = install_managed_symbol_config_scope_compat()
    assert result["installed"] is True
    configuration = normalize_configuration(
        account_id="binance-usdm:test",
        dual_side_payload={"dualSidePosition": True},
        symbol_configurations=(
            {
                "symbol": "币安人生USDT",
                "marginType": "cross",
                "leverage": 3,
            },
            {
                "symbol": "ETHUSDT",
                "marginType": "cross",
                "leverage": 5,
            },
        ),
        managed_symbols=("ETHUSDT",),
        observed_at=NOW,
    )
    assert configuration.active_margin_modes == ("cross",)
    assert configuration.leverage_by_symbol_side == {
        "ETHUSDT:LONG": 5,
        "ETHUSDT:SHORT": 5,
    }


def test_managed_symbol_config_remains_strict_after_scope_filter() -> None:
    from freqtrade.hedge.exchange.binance_normalizer import normalize_configuration
    from freqtrade.hedge.exchange.rate_limit import BinanceDataError
    from r25_toolkit.managed_symbol_config_compat import (
        install_managed_symbol_config_scope_compat,
    )

    install_managed_symbol_config_scope_compat()
    with pytest.raises(BinanceDataError, match="Invalid symbol configuration margin type"):
        normalize_configuration(
            account_id="binance-usdm:test",
            dual_side_payload={"dualSidePosition": True},
            symbol_configurations=(
                {
                    "symbol": "ETHUSDT",
                    "marginType": "portfolio",
                    "leverage": 5,
                },
            ),
            managed_symbols=("ETHUSDT",),
            observed_at=NOW,
        )

