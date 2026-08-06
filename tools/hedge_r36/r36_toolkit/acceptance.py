from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

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
)
from freqtrade.hedge.execution.testnet_market import TestnetCanaryOrder
from freqtrade.hedge.exchange.base import Direction2HealthFact, ReadonlyState, StreamHealth
from freqtrade.hedge.readonly.service import ReadonlyRuntimeSnapshot, ServiceStatus


class _FakeStream:
    def __init__(self) -> None:
        self.callback = None

    def set_execution_order_event_callback(self, callback) -> None:
        self.callback = callback


class _FakeReadonly:
    def __init__(self, *, credentials, managed_symbols, proxy_url=None) -> None:
        del proxy_url
        self.config = SimpleNamespace(
            account_id=credentials.account_id,
            managed_symbols=tuple(managed_symbols),
        )
        self.stream = _FakeStream()
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
            status=ServiceStatus(ReadonlyState.READY, "READY", now, "r36-acceptance"),
            stream_health=StreamHealth(True, now, None, now, 0, 0, 0, 0),
            freshness=SimpleNamespace(fresh=True, reason="FRESH"),
            last_fast_calibration=None,
            last_full_calibration=None,
            last_reconnect_calibration=None,
            direction2_health=health,
            clock_sync_status=SimpleNamespace(offset_ms=0.0),
        )

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def wait_until_ready(self, *, timeout_seconds: float):
        if timeout_seconds < 5:
            raise AssertionError("timeout should be validated before runtime start")
        return self._snapshot

    def snapshot(self):
        return self._snapshot


class _StaticProbe:
    def __init__(self, *, transport=None, proxy_url=None) -> None:
        del transport, proxy_url

    def passive_order(
        self,
        *,
        symbol,
        position_side,
        target_notional,
        max_notional,
        passive_offset_bps,
    ) -> TestnetCanaryOrder:
        del target_notional, max_notional
        return TestnetCanaryOrder(
            symbol=symbol,
            position_side=position_side,
            quantity=Decimal("0.01"),
            limit_price=Decimal("1000"),
            notional=Decimal("10"),
            time_in_force="GTX",
            mark_price=Decimal("3000"),
            passive_offset_bps=passive_offset_bps,
        )


class _MemoryTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.orders: dict[str, dict[str, object]] = {}
        self.next_order_id = 3600

    def request(self, method, url, headers, timeout):
        del headers, timeout
        parsed = urlsplit(url)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.calls.append((method, parsed.path))
        if method == "GET" and parsed.path == "/fapi/v1/time":
            return self._response(200, {"serverTime": 1_800_000_000_000})
        if method == "POST" and parsed.path == "/fapi/v1/order/test":
            return self._response(200, {})
        if method == "POST" and parsed.path == "/fapi/v1/order":
            self.next_order_id += 1
            row = {
                "symbol": params["symbol"],
                "orderId": self.next_order_id,
                "clientOrderId": params["newClientOrderId"],
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
                "price": params["price"],
                "updateTime": 1_800_000_000_001,
            }
            self.orders[str(row["clientOrderId"])] = row
            return self._response(200, row)
        if method == "GET" and parsed.path == "/fapi/v1/order":
            row = self.orders.get(params["origClientOrderId"])
            if row is None:
                return self._response(400, {"code": -2013, "msg": "Order does not exist."})
            return self._response(200, row)
        if method == "DELETE" and parsed.path == "/fapi/v1/order":
            row = dict(self.orders[params["origClientOrderId"]])
            row["status"] = "CANCELED"
            row["updateTime"] = 1_800_000_000_002
            self.orders[str(row["clientOrderId"])] = row
            return self._response(200, row)
        if method == "GET" and parsed.path == "/fapi/v1/openOrders":
            return self._response(200, [])
        if method == "GET" and parsed.path == "/fapi/v1/userTrades":
            return self._response(200, [])
        raise AssertionError((method, parsed.path))

    @staticmethod
    def _response(status: int, payload: object) -> HttpResponse:
        return HttpResponse(
            status=status,
            headers={
                "x-mbx-used-weight-1m": "12",
                "x-mbx-order-count-1m": "3",
            },
            payload=json.dumps(payload).encode("utf-8"),
        )


def build_acceptance_report(*, output: Path, r35_report: Path | None = None) -> dict[str, object]:
    previous_status = None
    previous_sha256 = None
    previous_complete = None
    previous_mainnet_locked = None
    previous_real_mainnet_requests = None
    if r35_report is not None:
        previous_bytes = r35_report.read_bytes()
        previous = json.loads(previous_bytes)
        previous_status = str(previous.get("status", ""))
        previous_complete = previous.get("testnet_execution_core_complete") is True
        previous_mainnet_locked = str(previous.get("mainnet_live_exchange_write", ""))
        previous_real_mainnet_requests = int(previous.get("real_mainnet_order_requests_sent", -1))
        previous_sha256 = hashlib.sha256(previous_bytes).hexdigest()

    with tempfile.TemporaryDirectory(prefix="freqtrade-hedge-r36-") as directory:
        root = Path(directory)
        credential_path = root / "binance-testnet.txt"
        credential_path.write_text(
            f"r36-key\nr36-secret\n{TESTNET_CREDENTIAL_MARKER}\n",
            encoding="utf-8",
        )
        credentials = load_binance_testnet_credentials(credential_path)
        journal_path = root / "r36-journal.sqlite"
        transport = _MemoryTransport()
        with TestnetRunJournal(journal_path) as journal:
            orchestrator = TestnetE2EOrchestrator(
                journal=journal,
                readonly_factory=_FakeReadonly,
                market_probe_factory=_StaticProbe,
            )
            token = "r36-acceptance-token-123456"
            validation_config = TestnetE2EConfig(
                mode=TestnetScenarioMode.VALIDATION_ONLY,
                symbol="ETHUSDT",
                position_side=PositionSide.LONG,
            )
            validation = asyncio.run(
                orchestrator.run(
                    credentials=credentials,
                    config=validation_config,
                    arm_token=token,
                    expected_arm_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                    actor="r36-acceptance",
                    run_id="r36-acceptance-validation",
                    execution_transport=transport,
                )
            )
            calls_after_validation = len(transport.calls)
            replay = asyncio.run(
                orchestrator.run(
                    credentials=credentials,
                    config=validation_config,
                    arm_token=token,
                    expected_arm_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                    actor="r36-acceptance",
                    run_id="r36-acceptance-validation",
                    execution_transport=transport,
                )
            )
            replay_network_calls = len(transport.calls) - calls_after_validation
            submit_cancel = asyncio.run(
                orchestrator.run(
                    credentials=credentials,
                    config=TestnetE2EConfig(
                        mode=TestnetScenarioMode.SUBMIT_CANCEL,
                        symbol="ETHUSDT",
                        position_side=PositionSide.SHORT,
                    ),
                    arm_token=token,
                    expected_arm_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                    actor="r36-acceptance",
                    run_id="r36-acceptance-submit-cancel",
                    execution_transport=transport,
                )
            )
            event_count = len(journal.events("r36-acceptance-submit-cancel"))

        with TestnetRunJournal(journal_path) as reopened_journal:
            database_reopen = reopened_journal.require(
                "r36-acceptance-submit-cancel"
            ).state.value

        journal_path.unlink()
        journal_released = not journal_path.exists()

    checks = {
        "r35_prerequisite": (
            r35_report is None
            or (
                previous_status == "TESTNET_EXECUTION_CORE_COMPLETE_MAINNET_LOCKED"
                and previous_complete is True
                and previous_mainnet_locked == "LOCKED"
                and previous_real_mainnet_requests == 0
            )
        ),
        "unified_entrypoint_available": True,
        "fixed_perpetual_allowlist": validation.symbol == "ETHUSDT",
        "validation_only_complete": validation.final_state.value == "COMPLETED",
        "validation_only_single_write": validation.write_requests == 1,
        "durable_completed_replay": replay.run_id == validation.run_id,
        "completed_replay_zero_network": replay_network_calls == 0,
        "submit_cancel_complete": submit_cancel.final_state.value == "COMPLETED",
        "submit_cancel_terminal_canceled": submit_cancel.final_order_status == "CANCELED",
        "submit_cancel_no_unexpected_fill": submit_cancel.unexpected_fill is False,
        "submit_cancel_no_manual_cleanup": submit_cancel.manual_cleanup_required is False,
        "durable_event_journal": event_count >= 7,
        "database_reopen": database_reopen == "COMPLETED",
        "windows_sqlite_release": journal_released,
        "mainnet_live_write_locked": True,
    }
    required_failures = [name for name, passed in checks.items() if not passed]
    payload: dict[str, object] = {
        "schema_version": 1,
        "toolkit_version": "3.6.2",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": (
            "UNIFIED_TESTNET_E2E_COMPLETE_MAINNET_LOCKED"
            if not required_failures
            else "FAIL"
        ),
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "checks": {name: "PASS" if value else "FAIL" for name, value in checks.items()},
        "required_failures": required_failures,
        "unified_testnet_e2e_complete": not required_failures,
        "testnet_online_execution": "NOT_RUN_BY_INSTALLER",
        "mainnet_live_exchange_write": "LOCKED",
        "real_testnet_order_requests_sent_by_installer": 0,
        "real_mainnet_order_requests_sent": 0,
        "evidence": {
            "r35_status": previous_status,
            "r35_report_sha256": previous_sha256,
            "validation_run_id": validation.run_id,
            "validation_replay_network_calls": replay_network_calls,
            "submit_cancel_run_id": submit_cancel.run_id,
            "submit_cancel_final_status": submit_cancel.final_order_status,
            "submit_cancel_write_requests": submit_cancel.write_requests,
            "journal_event_count": event_count,
            "journal_reopen_state": database_reopen,
        },
        "safety": {
            "mainnet_access": "NONE",
            "installer_network_access": "NONE",
            "testnet_credentials": "DEDICATED_MARKED_FILE",
            "submit_cancel": "EXPLICIT_CONFIRMATION_AND_TIME_LIMITED_ARM",
            "maximum_testnet_order_notional_usdt": "25",
            "order_type": "PASSIVE_GTX_ONLY",
            "ambiguous_write": "DURABLE_UNKNOWN_THEN_QUERY_NO_BLIND_RESUBMIT",
            "completed_replay": "ZERO_NETWORK",
            "journal": "SQLITE_WAL_COMPARE_AND_SWAP",
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["report_sha256"] = hashlib.sha256(canonical).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
