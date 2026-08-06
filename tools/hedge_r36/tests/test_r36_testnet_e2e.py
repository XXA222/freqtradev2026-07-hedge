from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from freqtrade.hedge.execution.binance_usdm_adapter import HttpResponse
from freqtrade.hedge.execution.service import PositionSide
from freqtrade.hedge.execution.testnet import (
    TESTNET_CREDENTIAL_MARKER,
    load_binance_testnet_credentials,
)
from freqtrade.hedge.execution.testnet_e2e import (
    TestnetE2EConfig,
    TestnetE2EOrchestrator,
    TestnetRunJournal,
    TestnetScenarioMode,
    TestnetScenarioState,
)
from freqtrade.hedge.execution.testnet_market import (
    BinanceTestnetMarketProbe,
    TestnetCanaryOrder,
)
from freqtrade.hedge.exchange.base import Direction2HealthFact, ReadonlyState, StreamHealth
from freqtrade.hedge.readonly.service import ReadonlyRuntimeSnapshot, ServiceStatus


class FakeStream:
    def __init__(self):
        self.callback = None

    def set_execution_order_event_callback(self, callback):
        self.callback = callback


class FakeReadonly:
    def __init__(self, *, credentials, managed_symbols, proxy_url=None):
        del proxy_url
        self.config = SimpleNamespace(
            account_id=credentials.account_id,
            managed_symbols=tuple(managed_symbols),
        )
        self.stream = FakeStream()
        self.started = False
        self.stopped = False
        now = datetime.now(UTC)
        health = Direction2HealthFact(
            account_id=credentials.account_id,
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
        )
        self._snapshot = ReadonlyRuntimeSnapshot(
            status=ServiceStatus(ReadonlyState.READY, "READY", now, "r36"),
            stream_health=StreamHealth(True, now, None, now, 0, 0, 0, 0),
            freshness=SimpleNamespace(fresh=True, reason="FRESH"),
            last_fast_calibration=None,
            last_full_calibration=None,
            last_reconnect_calibration=None,
            direction2_health=health,
            clock_sync_status=SimpleNamespace(offset_ms=4.2),
        )

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def wait_until_ready(self, *, timeout_seconds):
        assert timeout_seconds >= 5
        return self._snapshot

    def snapshot(self):
        return self._snapshot


class StaticProbe:
    def __init__(self, *, transport=None, proxy_url=None):
        del transport, proxy_url

    def passive_order(self, *, symbol, position_side, target_notional, max_notional, passive_offset_bps):
        del target_notional, max_notional
        return TestnetCanaryOrder(
            symbol=symbol,
            position_side=position_side,
            quantity=Decimal("0.001") if symbol == "BTCUSDT" else Decimal("0.01"),
            limit_price=Decimal("10000") if symbol == "BTCUSDT" else Decimal("1000"),
            notional=Decimal("10"),
            time_in_force="GTX",
            mark_price=Decimal("50000"),
            passive_offset_bps=passive_offset_bps,
        )


class MemoryTransport:
    def __init__(self, *, unexpected_fill=False, used_weight=10, order_count=3):
        self.calls = []
        self.orders = {}
        self.unexpected_fill = unexpected_fill
        self.used_weight = used_weight
        self.order_count = order_count
        self.next_order_id = 100

    def request(self, method, url, headers, timeout):
        del headers, timeout
        parsed = urlsplit(url)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.calls.append((method, parsed.path, params))
        if method == "GET" and parsed.path == "/fapi/v1/time":
            return self._response(200, {"serverTime": 1_800_000_000_000})
        if method == "POST" and parsed.path == "/fapi/v1/order/test":
            return self._response(200, {})
        if method == "POST" and parsed.path == "/fapi/v1/order":
            self.next_order_id += 1
            filled = params["quantity"] if self.unexpected_fill else "0"
            row = {
                "symbol": params["symbol"],
                "orderId": self.next_order_id,
                "clientOrderId": params["newClientOrderId"],
                "status": "FILLED" if self.unexpected_fill else "NEW",
                "executedQty": filled,
                "avgPrice": params.get("price", "0") if self.unexpected_fill else "0",
                "price": params.get("price", "0"),
                "updateTime": 1_800_000_000_000,
            }
            self.orders[row["clientOrderId"]] = row
            return self._response(200, row)
        if method == "DELETE" and parsed.path == "/fapi/v1/order":
            row = dict(self.orders[params["origClientOrderId"]])
            row["status"] = "CANCELED"
            row["updateTime"] = 1_800_000_000_001
            self.orders[row["clientOrderId"]] = row
            return self._response(200, row)
        if method == "GET" and parsed.path == "/fapi/v1/order":
            row = self.orders.get(params["origClientOrderId"])
            if row is None:
                return self._response(400, {"code": -2013, "msg": "not found"})
            return self._response(200, row)
        if method == "GET" and parsed.path == "/fapi/v1/openOrders":
            return self._response(200, [])
        if method == "GET" and parsed.path == "/fapi/v1/userTrades":
            return self._response(200, [])
        raise AssertionError((method, parsed.path))

    def _response(self, status, payload):
        return HttpResponse(
            status=status,
            headers={
                "x-mbx-used-weight-1m": str(self.used_weight),
                "x-mbx-order-count-1m": str(self.order_count),
            },
            payload=json.dumps(payload).encode(),
        )


class MarketTransport:
    def request(self, method, url, headers, timeout):
        del headers, timeout
        parsed = urlsplit(url)
        assert method == "GET"
        if parsed.path == "/fapi/v1/premiumIndex":
            payload = {"symbol": "ETHUSDT", "markPrice": "3000"}
        elif parsed.path == "/fapi/v1/exchangeInfo":
            payload = {
                "symbols": [
                    {
                        "symbol": "ETHUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.001",
                                "maxQty": "1000",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            {
                                "filterType": "PERCENT_PRICE",
                                "multiplierUp": "1.15",
                                "multiplierDown": "0.85",
                            },
                        ],
                    }
                ]
            }
        else:
            raise AssertionError(parsed.path)
        return HttpResponse(status=200, headers={}, payload=json.dumps(payload).encode())


def credentials(tmp_path: Path):
    path = tmp_path / "binance-testnet.txt"
    path.write_text(f"key\nsecret\n{TESTNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    return load_binance_testnet_credentials(path)


def orchestrator(tmp_path: Path):
    return TestnetE2EOrchestrator(
        journal=TestnetRunJournal(tmp_path / "r36.sqlite"),
        readonly_factory=FakeReadonly,
        market_probe_factory=StaticProbe,
    )


def test_config_is_fixed_to_mainstream_perpetual_symbols():
    assert TestnetE2EConfig(symbol="BTC/USDT:USDT").symbol == "BTCUSDT"
    with pytest.raises(ValueError, match="BTCUSDT and ETHUSDT"):
        TestnetE2EConfig(symbol="SOLUSDT")
    with pytest.raises(ValueError, match="25"):
        TestnetE2EConfig(max_order_notional=Decimal("26"))


def test_journal_is_idempotent_and_rejects_conflicting_run_id(tmp_path):
    journal = TestnetRunJournal(tmp_path / "journal.sqlite")
    config = TestnetE2EConfig()
    first, replay = journal.claim(
        run_id="same",
        request_hash=config.request_hash,
        account_id="binance-usdm-testnet:x",
        actor="pytest",
        config=config.canonical_payload(),
    )
    assert replay is False and first.state is TestnetScenarioState.NEW
    second, replay = journal.claim(
        run_id="same",
        request_hash=config.request_hash,
        account_id="binance-usdm-testnet:x",
        actor="pytest",
        config=config.canonical_payload(),
    )
    assert replay is True and second.version == 0
    with pytest.raises(RuntimeError, match="different input"):
        journal.claim(
            run_id="same",
            request_hash="0" * 64,
            account_id="binance-usdm-testnet:x",
            actor="pytest",
            config=config.canonical_payload(),
        )


def test_journal_enforces_state_machine(tmp_path):
    journal = TestnetRunJournal(tmp_path / "journal.sqlite")
    config = TestnetE2EConfig()
    journal.claim(
        run_id="flow",
        request_hash=config.request_hash,
        account_id="binance-usdm-testnet:x",
        actor="pytest",
        config=config.canonical_payload(),
    )
    with pytest.raises(RuntimeError, match="invalid R3.6 transition"):
        journal.transition(run_id="flow", target=TestnetScenarioState.COMPLETED)
    journal.transition(run_id="flow", target=TestnetScenarioState.PREFLIGHT)
    assert len(journal.events("flow")) == 2


def test_market_probe_builds_post_only_order_inside_filters():
    order = BinanceTestnetMarketProbe(transport=MarketTransport()).passive_order(
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        target_notional=Decimal("10"),
        max_notional=Decimal("25"),
        passive_offset_bps=1000,
    )
    assert order.time_in_force == "GTX"
    assert order.symbol == "ETHUSDT"
    assert order.limit_price < order.mark_price
    assert Decimal("5") <= order.notional <= Decimal("25")
    assert order.quantity % Decimal("0.001") == 0


def test_validation_only_is_durable_and_replay_sends_no_second_request(tmp_path):
    transport = MemoryTransport()
    runner = orchestrator(tmp_path)
    config = TestnetE2EConfig(mode=TestnetScenarioMode.VALIDATION_ONLY)
    first = asyncio.run(
        runner.run(
            credentials=credentials(tmp_path),
            config=config,
            arm_token="1234567890abcdef",
            expected_arm_token_sha256=hashlib.sha256(b"1234567890abcdef").hexdigest(),
            actor="pytest",
            run_id="validation",
            execution_transport=transport,
        )
    )
    call_count = len(transport.calls)
    second = asyncio.run(
        runner.run(
            credentials=credentials(tmp_path),
            config=config,
            arm_token="1234567890abcdef",
            expected_arm_token_sha256=hashlib.sha256(b"1234567890abcdef").hexdigest(),
            actor="pytest",
            run_id="validation",
            execution_transport=transport,
        )
    )
    assert first.status == "TESTNET_E2E_COMPLETE_MAINNET_LOCKED"
    assert first.write_requests == 1
    assert second.run_id == first.run_id
    assert len(transport.calls) == call_count


def test_submit_cancel_runs_validation_submit_cancel_and_verification(tmp_path):
    transport = MemoryTransport()
    result = asyncio.run(
        orchestrator(tmp_path).run(
            credentials=credentials(tmp_path),
            config=TestnetE2EConfig(mode=TestnetScenarioMode.SUBMIT_CANCEL),
            arm_token="1234567890abcdef",
            expected_arm_token_sha256=hashlib.sha256(b"1234567890abcdef").hexdigest(),
            actor="pytest",
            run_id="submit-cancel",
            execution_transport=transport,
        )
    )
    paths = [(method, path) for method, path, _ in transport.calls]
    assert ("POST", "/fapi/v1/order/test") in paths
    assert ("POST", "/fapi/v1/order") in paths
    assert ("DELETE", "/fapi/v1/order") in paths
    assert result.write_requests == 3
    assert result.final_order_status == "CANCELED"
    assert result.manual_cleanup_required is False


def test_unexpected_fill_fails_closed_and_is_journaled(tmp_path):
    transport = MemoryTransport(unexpected_fill=True)
    runner = orchestrator(tmp_path)
    with pytest.raises(RuntimeError, match="UNEXPECTED_FILL"):
        asyncio.run(
            runner.run(
                credentials=credentials(tmp_path),
                config=TestnetE2EConfig(mode=TestnetScenarioMode.SUBMIT_CANCEL),
                arm_token="1234567890abcdef",
                expected_arm_token_sha256=hashlib.sha256(b"1234567890abcdef").hexdigest(),
                actor="pytest",
                run_id="filled",
                execution_transport=transport,
            )
        )
    assert runner.journal.require("filled").state is TestnetScenarioState.FAILED


def test_rate_budget_is_fail_closed(tmp_path):
    transport = MemoryTransport(used_weight=2000)
    runner = orchestrator(tmp_path)
    with pytest.raises(RuntimeError, match="WEIGHT_BUDGET"):
        asyncio.run(
            runner.run(
                credentials=credentials(tmp_path),
                config=TestnetE2EConfig(
                    mode=TestnetScenarioMode.VALIDATION_ONLY,
                    max_used_weight_1m=1800,
                ),
                arm_token="1234567890abcdef",
                expected_arm_token_sha256=hashlib.sha256(b"1234567890abcdef").hexdigest(),
                actor="pytest",
                run_id="budget",
                execution_transport=transport,
            )
        )
    assert runner.journal.require("budget").state is TestnetScenarioState.FAILED


def test_journal_connection_is_released_for_windows_cleanup(tmp_path):
    path = tmp_path / "cleanup.sqlite"
    journal = TestnetRunJournal(path)
    config = TestnetE2EConfig()
    journal.claim(
        run_id="cleanup",
        request_hash=config.request_hash,
        account_id="binance-usdm-testnet:x",
        actor="pytest",
        config=config.canonical_payload(),
    )
    assert journal.require("cleanup").run_id == "cleanup"
    # Each journal operation must release its short-lived SQLite connection.
    path.unlink()
    assert not path.exists()
    journal.close()


def test_journal_context_manager_closes_and_rejects_reuse(tmp_path):
    path = tmp_path / "context.sqlite"
    config = TestnetE2EConfig()
    with TestnetRunJournal(path) as journal:
        journal.claim(
            run_id="context",
            request_hash=config.request_hash,
            account_id="binance-usdm-testnet:x",
            actor="pytest",
            config=config.canonical_payload(),
        )
        assert journal.require("context").run_id == "context"
    with pytest.raises(RuntimeError, match="journal is closed"):
        journal.require("context")
    path.unlink()
    assert not path.exists()


def test_make_testnet_intent_supports_gtx_and_rejects_other_tif(tmp_path):
    from freqtrade.hedge.execution.testnet import make_testnet_limit_intent

    value = credentials(tmp_path)
    intent = make_testnet_limit_intent(
        credentials=value,
        symbol="ETHUSDT",
        position_side=PositionSide.SHORT,
        quantity=Decimal("0.01"),
        limit_price=Decimal("1000"),
        idempotency_key="gtx",
        time_in_force="GTX",
    )
    assert intent.metadata["time_in_force"] == "GTX"
    with pytest.raises(ValueError, match="GTC or GTX"):
        make_testnet_limit_intent(
            credentials=value,
            symbol="ETHUSDT",
            position_side=PositionSide.SHORT,
            quantity=Decimal("0.01"),
            limit_price=Decimal("1000"),
            idempotency_key="ioc",
            time_in_force="IOC",
        )


def test_guarded_runtime_accepts_injected_execution_store(tmp_path):
    import hashlib

    from freqtrade.hedge.execution.idempotency import InMemoryIdempotencyStore
    from freqtrade.hedge.execution.production_gate import ProductionGateEvidence
    from freqtrade.hedge.execution.service import InMemoryExecutionStore
    from freqtrade.hedge.execution.testnet import build_guarded_testnet_runtime
    from freqtrade.hedge.execution.binance_environment import ExecutionEnvironment, TESTNET_PROFILE

    value = credentials(tmp_path)
    store = InMemoryExecutionStore()
    runtime = build_guarded_testnet_runtime(
        credentials=value,
        evidence=ProductionGateEvidence(
            environment=ExecutionEnvironment.TESTNET,
            account_fingerprint=value.account_fingerprint,
            account_id_prefix=TESTNET_PROFILE.account_prefix,
            allowed_symbols=("BTCUSDT", "ETHUSDT"),
            cross_margin_symbols=("BTCUSDT", "ETHUSDT"),
            readonly_status="FULL_PASS",
            user_stream_status="PASS_WITH_NO_USER_EVENTS",
            hedge_mode_enabled=True,
            clock_offset_ms=0,
            testnet_trading_enabled=True,
            futures_trading_permission=True,
            expected_arm_token_sha256=hashlib.sha256(b"1234567890abcdef").hexdigest(),
            max_order_notional=Decimal("25"),
            allow_market_orders=False,
        ),
        transport=MemoryTransport(),
        store=store,
        idempotency=InMemoryIdempotencyStore(),
    )
    assert runtime.store is store


def test_interrupted_submitted_run_recovers_by_query_and_cancel_without_resubmit(tmp_path):
    from freqtrade.hedge.execution.client_order_id import build_client_order_id
    from freqtrade.hedge.execution.testnet import make_testnet_limit_intent

    value = credentials(tmp_path)
    config = TestnetE2EConfig(mode=TestnetScenarioMode.SUBMIT_CANCEL)
    journal = TestnetRunJournal(tmp_path / "resume.sqlite")
    journal.claim(
        run_id="resume",
        request_hash=config.request_hash,
        account_id=value.account_id,
        actor="pytest",
        config=config.canonical_payload(),
    )
    for state in (
        TestnetScenarioState.PREFLIGHT,
        TestnetScenarioState.READONLY_READY,
        TestnetScenarioState.ARMED,
        TestnetScenarioState.VALIDATED,
    ):
        journal.transition(run_id="resume", target=state)
    intent = make_testnet_limit_intent(
        credentials=value,
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("0.01"),
        limit_price=Decimal("1000"),
        idempotency_key="r36:resume",
        time_in_force="GTX",
    )
    client_id = build_client_order_id(
        account_id=intent.account_id,
        symbol=intent.symbol,
        position_side=intent.position_side.value,
        idempotency_key=intent.idempotency_key,
    )
    intent_payload = {
        "account_id": intent.account_id,
        "symbol": intent.symbol,
        "position_side": intent.position_side.value,
        "action": intent.action.value,
        "quantity": str(intent.quantity),
        "idempotency_key": intent.idempotency_key,
        "order_type": intent.order_type.value,
        "limit_price": str(intent.limit_price),
        "reduce_only": intent.reduce_only,
        "intent_id": str(intent.intent_id),
        "action_group_id": None,
        "metadata": dict(intent.metadata),
    }
    journal.transition(
        run_id="resume",
        target=TestnetScenarioState.SUBMITTING,
        client_order_id=client_id,
        intent=intent_payload,
        last_order_status="UNKNOWN",
    )
    journal.transition(
        run_id="resume",
        target=TestnetScenarioState.SUBMITTED,
        client_order_id=client_id,
        intent=intent_payload,
        last_order_status="ACKNOWLEDGED",
    )
    transport = MemoryTransport()
    transport.orders[client_id] = {
        "symbol": "ETHUSDT",
        "orderId": 999,
        "clientOrderId": client_id,
        "status": "NEW",
        "executedQty": "0",
        "avgPrice": "0",
        "price": "1000",
        "updateTime": 1_800_000_000_000,
    }
    result = asyncio.run(
        TestnetE2EOrchestrator(
            journal=journal,
            readonly_factory=FakeReadonly,
            market_probe_factory=StaticProbe,
        ).run(
            credentials=value,
            config=config,
            arm_token="1234567890abcdef",
            expected_arm_token_sha256=hashlib.sha256(b"1234567890abcdef").hexdigest(),
            actor="pytest",
            run_id="resume",
            execution_transport=transport,
        )
    )
    paths = [(method, path) for method, path, _ in transport.calls]
    assert ("POST", "/fapi/v1/order") not in paths
    assert ("POST", "/fapi/v1/order/test") not in paths
    assert ("DELETE", "/fapi/v1/order") in paths
    assert result.resumed is True
    assert result.final_order_status == "CANCELED"


def test_offline_acceptance_is_complete_and_releases_sqlite(tmp_path):
    from tools.hedge_r36.r36_toolkit.acceptance import build_acceptance_report

    report = tmp_path / "acceptance.json"
    payload = build_acceptance_report(output=report)
    assert payload["status"] == "UNIFIED_TESTNET_E2E_COMPLETE_MAINNET_LOCKED"
    assert payload["unified_testnet_e2e_complete"] is True
    assert payload["real_testnet_order_requests_sent_by_installer"] == 0
    assert payload["real_mainnet_order_requests_sent"] == 0
    assert report.is_file()


def test_online_runner_requires_explicit_testnet_confirmation(tmp_path):
    from tools.hedge_r36.r36_toolkit.online import run_online

    with pytest.raises(PermissionError, match="confirmed-testnet"):
        run_online(
            credential_file=tmp_path / "missing.txt",
            journal_file=tmp_path / "journal.sqlite",
            report_file=tmp_path / "report.json",
            mode="validation-only",
            symbol="ETHUSDT",
            position_side="LONG",
            target_notional=Decimal("10"),
            max_order_notional=Decimal("25"),
            passive_offset_bps=1000,
            proxy_url=None,
            ready_timeout_seconds=120,
            arm_ttl_seconds=300,
            run_id="r36-confirmation",
            actor="pytest",
            confirmed_testnet=False,
            confirmed_submit_cancel=False,
        )
