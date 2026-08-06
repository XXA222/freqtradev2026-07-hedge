from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from freqtrade.hedge.contracts.ports import (
    AlwaysReadyGate,
    InMemoryPositionLock,
    InMemorySingleWriter,
    StaticMarketRules,
)
from freqtrade.hedge.execution.action_group import build_close_both_plan
from freqtrade.hedge.execution.action_group_store import InMemoryActionGroupRepository
from freqtrade.hedge.execution.binance_usdm_adapter import (
    BinanceExecutionCredentials,
    BinanceUSDMExecutionAdapter,
    HttpResponse,
)
from freqtrade.hedge.execution.event_publisher import InMemoryEventPublisher
from freqtrade.hedge.execution.fake_exchange import build_fake_execution_harness
from freqtrade.hedge.execution.idempotency import InMemoryIdempotencyStore
from freqtrade.hedge.execution.ledger import InMemoryExecutionLedger
from freqtrade.hedge.execution.production_gate import (
    ExecutionEnvironment,
    ExecutionWriteLockedError,
    ProductionExecutionGate,
    ProductionGateEvidence,
)
from freqtrade.hedge.execution.production_runtime import build_production_execution_runtime
from freqtrade.hedge.execution.service import (
    AllowAllRiskApproval,
    ApprovedOrderIntent,
    ExecutionService,
    InMemoryAuditLog,
    InMemoryExecutionStore,
    IntentAction,
    OrderIntent,
    OrderType,
    PositionSide,
)
from freqtrade.hedge.execution.state_machine import OrderState
from freqtrade.hedge.execution.unknown_resolver import UnknownOrderResolver
from freqtrade.hedge.execution.user_stream_bridge import (
    ExecutionUserStreamBridge,
    order_trade_update_snapshot,
)
from freqtrade.hedge.telemetry.metrics import HedgeMetrics


TOKEN = "r30-test-arm-token"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
ACCOUNT = "binance-usdm:abc123"


def evidence(*, environment=ExecutionEnvironment.TESTNET, **overrides):
    values = dict(
        environment=environment,
        account_fingerprint="abc123",
        allowed_symbols=("BTCUSDT", "ETHUSDT"),
        cross_margin_symbols=("BTCUSDT", "ETHUSDT"),
        readonly_status="FULL_PASS",
        user_stream_status="PASS_WITH_NO_USER_EVENTS",
        hedge_mode_enabled=True,
        clock_offset_ms=100,
        testnet_trading_enabled=True,
        expected_arm_token_sha256=TOKEN_HASH,
        max_order_notional=Decimal("100000"),
    )
    values.update(overrides)
    return ProductionGateEvidence(**values)


def arm_gate(**overrides):
    gate = ProductionExecutionGate(evidence(**overrides))
    gate.arm(token=TOKEN, actor="pytest", confirmed=True)
    return gate


def intent(
    *,
    side=PositionSide.LONG,
    action=IntentAction.OPEN,
    order_type=OrderType.LIMIT,
    quantity="0.01",
    price="1000",
    key="k1",
):
    return OrderIntent(
        account_id=ACCOUNT,
        symbol="BTCUSDT",
        position_side=side,
        action=action,
        quantity=Decimal(quantity),
        idempotency_key=key,
        order_type=order_type,
        limit_price=None if order_type is OrderType.MARKET else Decimal(price),
        metadata={"reference_price": Decimal(price)},
    )


def approved(value: OrderIntent, client_id="FTH-BTCUSDT-L0-ABCDEFGHIJKLMNOP"):
    return ApprovedOrderIntent(
        intent=value,
        approved_quantity=value.quantity,
        client_order_id=client_id,
        approved_at=datetime.now(UTC),
        risk_reason_codes=("TEST",),
    )


class MemoryTransport:
    def __init__(self, *, timeout_after_create=False):
        self.calls = []
        self.orders = {}
        self.timeout_after_create = timeout_after_create
        self._timed_out = False
        self.next_id = 100

    def request(self, method, url, headers, timeout):
        parsed = urlsplit(url)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.calls.append((method, parsed.path, params, dict(headers)))
        if parsed.path == "/fapi/v1/time":
            return self._json(200, {"serverTime": 1_800_000_000_000})
        if method == "POST" and parsed.path == "/fapi/v1/order":
            self.next_id += 1
            row = {
                "symbol": params["symbol"],
                "orderId": self.next_id,
                "clientOrderId": params["newClientOrderId"],
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
                "price": params.get("price", "0"),
                "updateTime": 1_800_000_000_100,
            }
            self.orders[row["clientOrderId"]] = row
            if self.timeout_after_create and not self._timed_out:
                self._timed_out = True
                raise TimeoutError("simulated timeout after matching engine accept")
            return self._json(200, row)
        if method == "GET" and parsed.path == "/fapi/v1/order":
            row = self.orders.get(params["origClientOrderId"])
            if row is None:
                return self._json(400, {"code": -2013, "msg": "Order does not exist."})
            return self._json(200, row)
        if method == "DELETE" and parsed.path == "/fapi/v1/order":
            row = dict(self.orders[params["origClientOrderId"]])
            row["status"] = "CANCELED"
            row["updateTime"] += 1
            self.orders[row["clientOrderId"]] = row
            return self._json(200, row)
        if method == "GET" and parsed.path == "/fapi/v1/openOrders":
            rows = [row for row in self.orders.values() if row["status"] == "NEW"]
            return self._json(200, rows)
        if method == "GET" and parsed.path == "/fapi/v1/userTrades":
            return self._json(200, [])
        raise AssertionError((method, parsed.path))

    @staticmethod
    def _json(status, payload):
        return HttpResponse(
            status=status,
            headers={"x-mbx-used-weight-1m": "12", "x-mbx-order-count-1m": "1"},
            payload=json.dumps(payload).encode(),
        )


def adapter(transport, store=None, gate=None):
    return BinanceUSDMExecutionAdapter(
        credentials=BinanceExecutionCredentials("key", "secret"),
        gate=gate or arm_gate(),
        store=store or InMemoryExecutionStore(),
        base_url="https://testnet.example",
        transport=transport,
        now_ms=lambda: 1_800_000_000_000,
    )


def test_live_gate_is_fail_closed_without_strict_key_and_ip_evidence():
    gate = ProductionExecutionGate(
        evidence(
            environment=ExecutionEnvironment.LIVE,
            testnet_trading_enabled=False,
            live_trading_enabled=True,
            futures_trading_permission=False,
            strict_key_policy_passed=False,
            ip_restricted=False,
        )
    )
    with pytest.raises(ExecutionWriteLockedError) as captured:
        gate.arm(token=TOKEN, actor="operator", confirmed=True)
    text = str(captured.value)
    assert "STRICT_KEY_POLICY_REQUIRED" in text
    assert "FUTURES_TRADING_PERMISSION_REQUIRED" in text
    assert "IP_RESTRICTION_REQUIRED" in text


def test_gate_is_process_local_time_limited_and_symbol_scoped():
    gate = arm_gate()
    permit = gate.assert_order_allowed(approved(intent()))
    assert permit.symbol == "BTCUSDT"
    outside = OrderIntent(
        account_id=ACCOUNT,
        symbol="SOLUSDT",
        position_side=PositionSide.LONG,
        action=IntentAction.OPEN,
        quantity=Decimal("1"),
        idempotency_key="sol",
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
    )
    with pytest.raises(ExecutionWriteLockedError, match="SYMBOL_NOT_ALLOWLISTED"):
        gate.assert_order_allowed(approved(outside))
    gate.disarm()
    with pytest.raises(ExecutionWriteLockedError, match="EXECUTION_NOT_ARMED"):
        gate.assert_order_allowed(approved(intent()))


@pytest.mark.parametrize(
    ("position_side", "action", "expected_side"),
    [
        (PositionSide.LONG, IntentAction.OPEN, "BUY"),
        (PositionSide.LONG, IntentAction.CLOSE, "SELL"),
        (PositionSide.SHORT, IntentAction.OPEN, "SELL"),
        (PositionSide.SHORT, IntentAction.CLOSE, "BUY"),
    ],
)
def test_binance_side_mapping_and_hedge_position_side(position_side, action, expected_side):
    store = InMemoryExecutionStore()
    transport = MemoryTransport()
    value = intent(side=position_side, action=action, key=f"{position_side}-{action}")
    item = approved(value)
    store.put(
        __import__("freqtrade.hedge.execution.service", fromlist=["ExecutionOrder"]).ExecutionOrder(
            intent=value,
            client_order_id=item.client_order_id,
            approved_quantity=value.quantity,
            lifecycle=__import__("freqtrade.hedge.execution.state_machine", fromlist=["OrderLifecycle"]).OrderLifecycle(updated_at=datetime.now(UTC)),
            created_at=datetime.now(UTC),
        )
    )
    result = adapter(transport, store=store).submit_order(item)
    assert result.status is OrderState.ACKNOWLEDGED
    post = next(call for call in transport.calls if call[0] == "POST")
    assert post[2]["side"] == expected_side
    assert post[2]["positionSide"] == position_side.value
    assert "reduceOnly" not in post[2]


def test_submit_timeout_is_unknown_then_resolved_without_resubmit():
    store = InMemoryExecutionStore()
    transport = MemoryTransport(timeout_after_create=True)
    exchange = adapter(transport, store=store)
    service = ExecutionService(
        risk=AllowAllRiskApproval(),
        exchange=exchange,
        store=store,
        idempotency=InMemoryIdempotencyStore(),
        unknown_resolver=UnknownOrderResolver(exchange),
        kill_switch=__import__("freqtrade.hedge.execution.kill_switch", fromlist=["KillSwitch"]).KillSwitch(),
        audit=InMemoryAuditLog(),
        metrics=HedgeMetrics(),
    )
    result = service.submit(intent(key="timeout"))
    assert result.order.lifecycle.status is OrderState.ACKNOWLEDGED
    post_calls = [call for call in transport.calls if call[0] == "POST"]
    query_calls = [call for call in transport.calls if call[0] == "GET" and call[1] == "/fapi/v1/order"]
    assert len(post_calls) == 1
    assert len(query_calls) >= 1


def test_cancel_remains_available_after_gate_is_disarmed():
    store = InMemoryExecutionStore()
    transport = MemoryTransport()
    gate = arm_gate()
    exchange = adapter(transport, store=store, gate=gate)
    service = ExecutionService(
        risk=AllowAllRiskApproval(),
        exchange=exchange,
        store=store,
        idempotency=InMemoryIdempotencyStore(),
        unknown_resolver=UnknownOrderResolver(exchange),
        kill_switch=__import__("freqtrade.hedge.execution.kill_switch", fromlist=["KillSwitch"]).KillSwitch(),
    )
    first = service.submit(intent(key="cancel-after-disarm"))
    gate.disarm()
    canceled = service.cancel(first.order.client_order_id)
    assert canceled.order.lifecycle.status is OrderState.CANCELED
    assert [call[0] for call in transport.calls].count("DELETE") == 1


def test_cancel_is_confirmed_before_replacement_risk_can_overlap():
    store = InMemoryExecutionStore()
    transport = MemoryTransport()
    exchange = adapter(transport, store=store)
    service = ExecutionService(
        risk=AllowAllRiskApproval(),
        exchange=exchange,
        store=store,
        idempotency=InMemoryIdempotencyStore(),
        unknown_resolver=UnknownOrderResolver(exchange),
        kill_switch=__import__("freqtrade.hedge.execution.kill_switch", fromlist=["KillSwitch"]).KillSwitch(),
    )
    first = service.submit(intent(key="first"))
    canceled = service.cancel(first.order.client_order_id)
    assert canceled.order.lifecycle.status is OrderState.CANCELED
    assert [call[0] for call in transport.calls].count("POST") == 1
    assert [call[0] for call in transport.calls].count("DELETE") == 1


def test_user_stream_bridge_applies_partial_and_fill_to_known_order():
    harness = build_fake_execution_harness()
    submitted = harness.service.submit(intent(key="stream"))
    bridge = ExecutionUserStreamBridge(
        engine=__import__("freqtrade.hedge.execution.orchestrator", fromlist=["HedgeExecutionEngine"]).HedgeExecutionEngine(harness.service),
        cache=__import__("freqtrade.hedge.execution.unknown_resolver", fromlist=["InMemoryUserStreamOrderCache"]).InMemoryUserStreamOrderCache(),
        allowed_symbols=("BTCUSDT", "ETHUSDT"),
    )
    base = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1_800_000_000_000,
        "o": {
            "s": "BTCUSDT",
            "c": submitted.order.client_order_id,
            "i": 123,
            "X": "PARTIALLY_FILLED",
            "z": "0.004",
            "ap": "1000",
            "t": 1,
            "n": "0.01",
            "N": "USDT",
        },
    }
    partial = bridge.handle(base)
    assert partial is not None
    assert partial.order.lifecycle.status is OrderState.PARTIAL
    base["E"] += 1
    base["o"] = dict(base["o"], X="FILLED", z="0.01", t=2)
    filled = bridge.handle(base)
    assert filled is not None
    assert filled.order.lifecycle.status is OrderState.FILLED


def test_non_whitelisted_user_stream_symbol_is_ignored_before_parsing():
    event = {"e": "ORDER_TRADE_UPDATE", "o": {"s": "SOLUSDT"}}
    assert order_trade_update_snapshot(event, allowed_symbols=("BTCUSDT", "ETHUSDT")) is None


def test_close_both_builder_creates_independent_reduce_only_legs():
    record, intents = build_close_both_plan(
        account_id=ACCOUNT,
        symbol="BTCUSDT",
        long_quantity=Decimal("0.1"),
        short_quantity=Decimal("0.2"),
        idempotency_key="close-both",
        order_type=OrderType.MARKET,
    )
    assert record.action_type == "CLOSE_BOTH"
    assert {item.position_side for item in intents} == {PositionSide.LONG, PositionSide.SHORT}
    assert all(item.reduce_only and item.action is IntentAction.CLOSE for item in intents)


def test_kill_switch_blocks_increase_but_allows_reduce():
    harness = build_fake_execution_harness()
    harness.kill_switch.activate(reason="operator halt", actor="pytest")
    with pytest.raises(Exception):
        harness.service.submit(intent(key="blocked"))
    reduce_intent = intent(action=IntentAction.CLOSE, key="reduce")
    result = harness.service.submit(reduce_intent)
    assert result.order.lifecycle.status is OrderState.ACKNOWLEDGED


def test_production_runtime_composes_all_explicit_ports():
    store = InMemoryExecutionStore()
    runtime = build_production_execution_runtime(
        credentials=BinanceExecutionCredentials("key", "secret"),
        gate=arm_gate(),
        risk=AllowAllRiskApproval(),
        store=store,
        idempotency=InMemoryIdempotencyStore(),
        readiness=AlwaysReadyGate(),
        single_writer=InMemorySingleWriter(),
        position_lock=InMemoryPositionLock(),
        market_rules=StaticMarketRules(),
        transaction=InMemoryExecutionLedger(),
        publisher=InMemoryEventPublisher(),
        action_group_repository=InMemoryActionGroupRepository(),
        base_url="https://testnet.example",
        transport=MemoryTransport(),
    )
    result = runtime.engine.submit(intent(key="runtime"))
    assert result.order.lifecycle.status is OrderState.ACKNOWLEDGED
    assert runtime.exchange.telemetry().write_requests == 1




class CapturingUserStream:
    def __init__(self):
        self.callback = None

    def set_execution_order_event_callback(self, callback):
        self.callback = callback


def test_production_runtime_binds_execution_bridge_to_real_stream_hook():
    stream = CapturingUserStream()
    runtime = build_production_execution_runtime(
        credentials=BinanceExecutionCredentials("key", "secret"),
        gate=arm_gate(),
        risk=AllowAllRiskApproval(),
        store=InMemoryExecutionStore(),
        idempotency=InMemoryIdempotencyStore(),
        readiness=AlwaysReadyGate(),
        single_writer=InMemorySingleWriter(),
        position_lock=InMemoryPositionLock(),
        market_rules=StaticMarketRules(),
        transaction=InMemoryExecutionLedger(),
        publisher=InMemoryEventPublisher(),
        action_group_repository=InMemoryActionGroupRepository(),
        base_url="https://testnet.example",
        transport=MemoryTransport(),
        user_stream=stream,
    )
    assert stream.callback == runtime.user_stream_bridge.handle

def test_credentials_repr_never_contains_secret():
    value = BinanceExecutionCredentials("visible-key", "very-secret")
    text = repr(value)
    assert "visible-key" not in text
    assert "very-secret" not in text
    assert "redacted" in text
