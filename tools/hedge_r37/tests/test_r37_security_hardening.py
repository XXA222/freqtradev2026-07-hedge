from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from freqtrade.hedge.control.models import ControlAction, ControlRequest
from freqtrade.hedge.control.store import (
    ControlOperationConflict,
    InMemoryControlOperationStore,
)
from freqtrade.hedge.execution.binance_environment import (
    ExecutionEnvironment,
    validate_rest_base_url,
)
from freqtrade.hedge.execution.binance_usdm_adapter import UrlLibHttpTransport
from freqtrade.hedge.execution.client_order_id import build_client_order_id
from freqtrade.hedge.execution.service import PositionSide
from freqtrade.hedge.execution.testnet import make_testnet_limit_intent
from freqtrade.hedge.execution.testnet_e2e import (
    TestnetE2EConfig,
    TestnetE2EOrchestrator,
    TestnetRunJournal,
    TestnetScenarioMode,
    TestnetScenarioState,
)
from tools.hedge_r36.tests.test_r36_testnet_e2e import (
    FakeReadonly,
    MemoryTransport,
    StaticProbe,
    credentials,
)

TOKEN = "1234567890abcdef"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


def _intent_payload(intent):
    return {
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


def _submitting_journal(tmp_path: Path, *, actor: str = "alice"):
    value = credentials(tmp_path)
    config = TestnetE2EConfig(mode=TestnetScenarioMode.SUBMIT_CANCEL)
    journal = TestnetRunJournal(tmp_path / "submitting.sqlite")
    journal.claim(
        run_id="crash-window",
        request_hash=config.request_hash,
        account_id=value.account_id,
        actor=actor,
        config=config.canonical_payload(),
    )
    for state in (
        TestnetScenarioState.PREFLIGHT,
        TestnetScenarioState.READONLY_READY,
        TestnetScenarioState.ARMED,
        TestnetScenarioState.VALIDATED,
    ):
        journal.transition(run_id="crash-window", target=state)
    intent = make_testnet_limit_intent(
        credentials=value,
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("0.01"),
        limit_price=Decimal("1000"),
        idempotency_key="r36:crash-window",
        time_in_force="GTX",
    )
    client_id = build_client_order_id(
        account_id=intent.account_id,
        symbol=intent.symbol,
        position_side=intent.position_side.value,
        idempotency_key=intent.idempotency_key,
    )
    journal.transition(
        run_id="crash-window",
        target=TestnetScenarioState.SUBMITTING,
        client_order_id=client_id,
        intent=_intent_payload(intent),
        last_order_status="UNKNOWN",
    )
    return value, config, journal, client_id


def test_arm_token_cannot_self_authorize(tmp_path):
    runner = TestnetE2EOrchestrator(
        journal=TestnetRunJournal(tmp_path / "arm.sqlite"),
        readonly_factory=FakeReadonly,
        market_probe_factory=StaticProbe,
    )
    with pytest.raises(PermissionError, match="independently configured"):
        asyncio.run(
            runner.run(
                credentials=credentials(tmp_path),
                config=TestnetE2EConfig(),
                arm_token=TOKEN,
                expected_arm_token_sha256=hashlib.sha256(b"different-token-123").hexdigest(),
                actor="alice",
                run_id="arm-mismatch",
                execution_transport=MemoryTransport(),
            )
        )


def test_run_id_is_bound_to_actor(tmp_path):
    value = credentials(tmp_path)
    config = TestnetE2EConfig()
    journal = TestnetRunJournal(tmp_path / "actor.sqlite")
    journal.claim(
        run_id="same",
        request_hash=config.request_hash,
        account_id=value.account_id,
        actor="alice",
        config=config.canonical_payload(),
    )
    with pytest.raises(RuntimeError, match="actor"):
        journal.claim(
            run_id="same",
            request_hash=config.request_hash,
            account_id=value.account_id,
            actor="bob",
            config=config.canonical_payload(),
        )


def test_interrupted_submitting_never_resubmits_when_order_is_not_found(tmp_path):
    value, config, journal, _ = _submitting_journal(tmp_path)
    transport = MemoryTransport()
    report = asyncio.run(
        TestnetE2EOrchestrator(
            journal=journal,
            readonly_factory=FakeReadonly,
            market_probe_factory=StaticProbe,
        ).run(
            credentials=value,
            config=config,
            arm_token=TOKEN,
            expected_arm_token_sha256=TOKEN_HASH,
            actor="alice",
            run_id="crash-window",
            execution_transport=transport,
        )
    )
    paths = [(method, path) for method, path, _ in transport.calls]
    assert ("POST", "/fapi/v1/order") not in paths
    assert report.final_state is TestnetScenarioState.RECOVERY_REQUIRED
    assert report.manual_cleanup_required is True
    assert journal.require("crash-window").state is TestnetScenarioState.RECOVERY_REQUIRED


def test_interrupted_submitting_queries_existing_order_and_cancels_without_resubmit(tmp_path):
    value, config, journal, client_id = _submitting_journal(tmp_path)
    transport = MemoryTransport()
    transport.orders[client_id] = {
        "symbol": "ETHUSDT",
        "orderId": 777,
        "clientOrderId": client_id,
        "status": "NEW",
        "executedQty": "0",
        "avgPrice": "0",
        "price": "1000",
        "updateTime": 1_800_000_000_000,
    }
    report = asyncio.run(
        TestnetE2EOrchestrator(
            journal=journal,
            readonly_factory=FakeReadonly,
            market_probe_factory=StaticProbe,
        ).run(
            credentials=value,
            config=config,
            arm_token=TOKEN,
            expected_arm_token_sha256=TOKEN_HASH,
            actor="alice",
            run_id="crash-window",
            execution_transport=transport,
        )
    )
    paths = [(method, path) for method, path, _ in transport.calls]
    assert ("POST", "/fapi/v1/order") not in paths
    assert ("DELETE", "/fapi/v1/order") in paths
    assert report.final_order_status == "CANCELED"


def test_authenticated_transport_rejects_redirect_without_forwarding_api_key():
    observed = {"target_called": False, "api_key": None}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            observed["target_called"] = True
            observed["api_key"] = self.headers.get("X-MBX-APIKEY")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
            self.end_headers()

        def log_message(self, *_):
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    source_thread = threading.Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    try:
        response = UrlLibHttpTransport().request(
            "GET",
            f"http://127.0.0.1:{source.server_port}/signed?signature=secret",
            {"X-MBX-APIKEY": "api-key"},
            2,
        )
        assert response.status == 302
        assert observed == {"target_called": False, "api_key": None}
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()


def test_rest_base_url_rejects_non_root_path():
    with pytest.raises(ValueError, match="path must be empty"):
        validate_rest_base_url(
            ExecutionEnvironment.TESTNET,
            "https://demo-fapi.binance.com/evil-prefix",
        )


def _request(key="lease"):
    return ControlRequest(
        action=ControlAction.STOP_NEW_ORDERS,
        account_id="binance-usdm:test",
        idempotency_key=key,
        reason="security-test",
    )


def test_stale_control_operation_lease_is_recoverable_only_by_same_actor():
    store = InMemoryControlOperationStore(lease_seconds=5, owner="worker-a")
    request = _request()
    first = store.claim(request=request, actor="alice", actor_role="OPERATOR")
    record = store._records[(request.account_id, request.idempotency_key)]
    record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    recovered = store.claim(request=request, actor="alice", actor_role="OPERATOR")
    assert recovered.operation_id == first.operation_id
    assert recovered.recovered_stale is True

    record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ControlOperationConflict, match="actor mismatch"):
        store.claim(request=request, actor="bob", actor_role="ADMIN")


def test_legacy_completed_run_can_replay_but_active_run_requires_manual_recovery(tmp_path):
    value = credentials(tmp_path)
    config = TestnetE2EConfig()
    journal = TestnetRunJournal(tmp_path / "legacy.sqlite")
    journal.claim(
        run_id="legacy-active",
        request_hash=config.request_hash,
        account_id=value.account_id,
        actor="original",
        config=config.canonical_payload(),
    )
    with journal._connect() as connection:
        connection.execute("UPDATE r36_testnet_runs SET actor='' WHERE run_id='legacy-active'")
    with pytest.raises(RuntimeError, match="manual recovery"):
        journal.claim(
            run_id="legacy-active",
            request_hash=config.request_hash,
            account_id=value.account_id,
            actor="new-actor",
            config=config.canonical_payload(),
        )

    journal.claim(
        run_id="legacy-complete",
        request_hash=config.request_hash,
        account_id=value.account_id,
        actor="original",
        config=config.canonical_payload(),
    )
    with journal._connect() as connection:
        connection.execute(
            "UPDATE r36_testnet_runs SET actor='', state=?, report_json=? WHERE run_id=?",
            (
                TestnetScenarioState.COMPLETED.value,
                '{"status":"TESTNET_E2E_COMPLETE_MAINNET_LOCKED"}',
                "legacy-complete",
            ),
        )
    record, replay = journal.claim(
        run_id="legacy-complete",
        request_hash=config.request_hash,
        account_id=value.account_id,
        actor="new-actor",
        config=config.canonical_payload(),
    )
    assert replay is True
    assert record.actor == ""
    assert record.state is TestnetScenarioState.COMPLETED
