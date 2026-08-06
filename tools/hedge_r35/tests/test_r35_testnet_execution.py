from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from freqtrade.hedge.execution.binance_environment import (
    ExecutionEnvironment,
    TESTNET_PROFILE,
    execution_account_id,
    validate_rest_base_url,
    validate_websocket_base_url,
)
from freqtrade.hedge.execution.binance_usdm_adapter import HttpResponse
from freqtrade.hedge.execution.production_gate import ProductionGateEvidence
from freqtrade.hedge.execution.service import OrderState, PositionSide
from freqtrade.hedge.execution.testnet import (
    TESTNET_ALLOWED_SYMBOLS,
    TESTNET_CREDENTIAL_MARKER,
    BinanceTestnetCredentials,
    build_guarded_testnet_runtime,
    build_testnet_readonly_config,
    evidence_from_ready_testnet_readonly,
    load_binance_testnet_credentials,
    make_testnet_limit_intent,
    run_submit_cancel_canary,
)
from freqtrade.hedge.exchange.base import Direction2HealthFact, ReadonlyState, StreamHealth
from freqtrade.hedge.readonly.service import ReadonlyRuntimeSnapshot, ServiceStatus

TOKEN = "r35-arm-token"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


class MemoryTestnetTransport:
    def __init__(self, *, validation_timeout: bool = False, unexpected_fill: bool = False):
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.orders: dict[str, dict[str, object]] = {}
        self.validation_timeout = validation_timeout
        self.unexpected_fill = unexpected_fill
        self.next_order_id = 1000

    def request(self, method, url, headers, timeout):
        del headers, timeout
        parsed = urlsplit(url)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.calls.append((method, parsed.path, params))
        if method == "POST" and parsed.path == "/fapi/v1/order/test":
            if self.validation_timeout:
                raise TimeoutError("unknown validation outcome")
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
            self.orders[str(row["clientOrderId"])] = row
            return self._response(200, row)
        if method == "DELETE" and parsed.path == "/fapi/v1/order":
            current = dict(self.orders[params["origClientOrderId"]])
            current["status"] = "CANCELED"
            current["updateTime"] = 1_800_000_000_001
            self.orders[str(current["clientOrderId"])] = current
            return self._response(200, current)
        if method == "GET" and parsed.path == "/fapi/v1/order":
            current = self.orders.get(params["origClientOrderId"])
            if current is None:
                return self._response(400, {"code": -2013, "msg": "not found"})
            return self._response(200, current)
        if method == "GET" and parsed.path == "/fapi/v1/openOrders":
            return self._response(200, [])
        if method == "GET" and parsed.path == "/fapi/v1/userTrades":
            return self._response(200, [])
        if method == "GET" and parsed.path == "/fapi/v1/time":
            return self._response(200, {"serverTime": 1_800_000_000_000})
        raise AssertionError((method, parsed.path))

    @staticmethod
    def _response(status: int, payload: object) -> HttpResponse:
        return HttpResponse(
            status=status,
            headers={"x-mbx-used-weight-1m": "1", "x-mbx-order-count-1m": "1"},
            payload=json.dumps(payload).encode(),
        )


def credentials(tmp_path: Path) -> BinanceTestnetCredentials:
    path = tmp_path / "binance-testnet.txt"
    path.write_text(f"test-key\ntest-secret\n{TESTNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    return load_binance_testnet_credentials(path)


def evidence(value: BinanceTestnetCredentials) -> ProductionGateEvidence:
    return ProductionGateEvidence(
        environment=ExecutionEnvironment.TESTNET,
        account_fingerprint=value.account_fingerprint,
        account_id_prefix=TESTNET_PROFILE.account_prefix,
        allowed_symbols=TESTNET_ALLOWED_SYMBOLS,
        cross_margin_symbols=TESTNET_ALLOWED_SYMBOLS,
        readonly_status="FULL_PASS",
        user_stream_status="PASS_WITH_NO_USER_EVENTS",
        hedge_mode_enabled=True,
        clock_offset_ms=10,
        testnet_trading_enabled=True,
        futures_trading_permission=True,
        expected_arm_token_sha256=TOKEN_HASH,
        max_order_notional=Decimal("25"),
        allow_market_orders=False,
    )


def runtime(tmp_path: Path, transport: MemoryTestnetTransport | None = None):
    value = credentials(tmp_path)
    guarded = build_guarded_testnet_runtime(
        credentials=value,
        evidence=evidence(value),
        transport=transport or MemoryTestnetTransport(),
    )
    guarded.arm(token=TOKEN, actor="pytest")
    return value, guarded


def test_testnet_credentials_require_explicit_marker(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("key\nsecret\nNOT_TESTNET\n", encoding="utf-8")
    with pytest.raises(ValueError, match="marker"):
        load_binance_testnet_credentials(path)


def test_testnet_credentials_are_redacted_and_namespaced(tmp_path):
    value = credentials(tmp_path)
    text = repr(value)
    assert "test-key" not in text and "test-secret" not in text
    assert value.account_id.startswith("binance-usdm-testnet:")
    assert execution_account_id(ExecutionEnvironment.TESTNET, value.account_fingerprint) == value.account_id


def test_testnet_endpoint_profiles_are_strict():
    assert validate_rest_base_url(ExecutionEnvironment.TESTNET, TESTNET_PROFILE.rest_base_url) == TESTNET_PROFILE.rest_base_url
    assert validate_websocket_base_url(ExecutionEnvironment.TESTNET, TESTNET_PROFILE.websocket_base_url) == TESTNET_PROFILE.websocket_base_url
    with pytest.raises(ValueError):
        validate_rest_base_url(ExecutionEnvironment.TESTNET, "https://fapi.binance.com")
    with pytest.raises(ValueError):
        validate_websocket_base_url(ExecutionEnvironment.TESTNET, "wss://fstream.binance.com/ws")


def test_readonly_testnet_config_uses_demo_endpoints_and_fixed_symbols(tmp_path):
    config = build_testnet_readonly_config(credentials=credentials(tmp_path))
    assert config.futures_base_url == "https://demo-fapi.binance.com"
    assert config.websocket_base_url == "wss://demo-fstream.binance.com/ws"
    assert config.managed_symbols == TESTNET_ALLOWED_SYMBOLS
    with pytest.raises(ValueError):
        build_testnet_readonly_config(credentials=credentials(tmp_path), managed_symbols=("SOLUSDT",))


def test_testnet_runtime_rejects_legacy_mainnet_namespace(tmp_path):
    value = credentials(tmp_path)
    bad = ProductionGateEvidence(
        environment=ExecutionEnvironment.TESTNET,
        account_fingerprint=value.account_fingerprint,
        account_id_prefix="binance-usdm",
        allowed_symbols=TESTNET_ALLOWED_SYMBOLS,
        cross_margin_symbols=TESTNET_ALLOWED_SYMBOLS,
        readonly_status="FULL_PASS",
        user_stream_status="PASS_WITH_NO_USER_EVENTS",
        hedge_mode_enabled=True,
        clock_offset_ms=0,
        testnet_trading_enabled=True,
        futures_trading_permission=True,
        expected_arm_token_sha256=TOKEN_HASH,
        max_order_notional=Decimal("25"),
    )
    with pytest.raises(ValueError, match="isolated testnet account namespace"):
        build_guarded_testnet_runtime(credentials=value, evidence=bad, transport=MemoryTestnetTransport())


def test_order_test_validation_uses_gate_and_does_not_create_exchange_order(tmp_path):
    transport = MemoryTestnetTransport()
    value, guarded = runtime(tmp_path, transport)
    intent = make_testnet_limit_intent(
        credentials=value,
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("0.001"),
        limit_price=Decimal("10000"),
        idempotency_key="validate-only",
    )
    result = guarded.validate_order(intent)
    assert result.accepted is True
    assert [(method, path) for method, path, _ in transport.calls] == [("POST", "/fapi/v1/order/test")]
    assert guarded.store.list_orders() == ()


def test_validation_timeout_is_fail_closed(tmp_path):
    value, guarded = runtime(tmp_path, MemoryTestnetTransport(validation_timeout=True))
    intent = make_testnet_limit_intent(
        credentials=value,
        symbol="ETHUSDT",
        position_side=PositionSide.SHORT,
        quantity=Decimal("0.01"),
        limit_price=Decimal("1000"),
        idempotency_key="timeout",
    )
    with pytest.raises(TimeoutError, match="unknown"):
        guarded.validate_order(intent)


def test_submit_cancel_canary_uses_two_writes_and_finishes_canceled(tmp_path):
    transport = MemoryTestnetTransport()
    value, guarded = runtime(tmp_path, transport)
    intent = make_testnet_limit_intent(
        credentials=value,
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("0.001"),
        limit_price=Decimal("10000"),
        idempotency_key="submit-cancel",
    )
    report = run_submit_cancel_canary(guarded=guarded, intent=intent)
    assert report.clean is True
    assert report.write_requests == 2
    assert report.cancel_status == OrderState.CANCELED.value
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("POST", "/fapi/v1/order"),
        ("DELETE", "/fapi/v1/order"),
    ]


def test_unexpected_fill_is_reported_and_not_hidden(tmp_path):
    transport = MemoryTestnetTransport(unexpected_fill=True)
    value, guarded = runtime(tmp_path, transport)
    intent = make_testnet_limit_intent(
        credentials=value,
        symbol="ETHUSDT",
        position_side=PositionSide.SHORT,
        quantity=Decimal("0.01"),
        limit_price=Decimal("1000"),
        idempotency_key="unexpected-fill",
    )
    report = run_submit_cancel_canary(guarded=guarded, intent=intent)
    assert report.unexpected_fill is True
    assert report.clean is False
    assert report.submitted_status == OrderState.FILLED.value
    assert report.write_requests == 1


def test_max_notional_and_market_orders_remain_locked(tmp_path):
    value, guarded = runtime(tmp_path)
    too_large = make_testnet_limit_intent(
        credentials=value,
        symbol="BTCUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("10000"),
        idempotency_key="too-large",
    )
    with pytest.raises(PermissionError, match="ORDER_NOTIONAL_LIMIT_EXCEEDED"):
        guarded.validate_order(too_large)


def test_ready_readonly_evidence_requires_all_health_gates(tmp_path):
    value = credentials(tmp_path)
    now = datetime.now(UTC)
    health = Direction2HealthFact(
        account_id=value.account_id,
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
    snapshot = ReadonlyRuntimeSnapshot(
        status=ServiceStatus(ReadonlyState.READY, "READY", now, "run"),
        stream_health=StreamHealth(True, now, None, now, 0, 0, 0, 0),
        freshness=SimpleNamespace(fresh=True, reason="FRESH"),
        last_fast_calibration=None,
        last_full_calibration=None,
        last_reconnect_calibration=None,
        direction2_health=health,
        clock_sync_status=SimpleNamespace(offset_ms=12.4),
    )
    readonly = SimpleNamespace(
        snapshot=lambda: snapshot,
        config=SimpleNamespace(account_id=value.account_id, managed_symbols=TESTNET_ALLOWED_SYMBOLS),
    )
    result = evidence_from_ready_testnet_readonly(
        readonly=readonly,
        credentials=value,
        expected_arm_token_sha256=TOKEN_HASH,
    )
    assert result.account_id_prefix == "binance-usdm-testnet"
    assert result.clock_offset_ms == 12
    assert result.futures_trading_permission is True


def test_testnet_evidence_rejects_missing_trading_permission(tmp_path):
    value = credentials(tmp_path)
    readonly = SimpleNamespace(snapshot=lambda: None)
    with pytest.raises(RuntimeError, match="FUTURES_TRADING_PERMISSION"):
        evidence_from_ready_testnet_readonly(
            readonly=readonly,
            credentials=value,
            expected_arm_token_sha256=TOKEN_HASH,
            futures_trading_permission=False,
        )
