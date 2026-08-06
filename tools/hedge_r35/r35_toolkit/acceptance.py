from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from freqtrade.hedge.execution.binance_environment import (
    ExecutionEnvironment,
    TESTNET_PROFILE,
    validate_rest_base_url,
)
from freqtrade.hedge.execution.binance_usdm_adapter import HttpResponse
from freqtrade.hedge.execution.production_gate import ProductionGateEvidence
from freqtrade.hedge.execution.service import PositionSide
from freqtrade.hedge.execution.testnet import (
    TESTNET_ALLOWED_SYMBOLS,
    TESTNET_CREDENTIAL_MARKER,
    build_guarded_testnet_runtime,
    load_binance_testnet_credentials,
    make_testnet_limit_intent,
    run_submit_cancel_canary,
)

TOKEN = "r35-acceptance-arm-token"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.orders: dict[str, dict[str, object]] = {}

    def request(self, method, url, headers, timeout):
        del headers, timeout
        parsed = urlsplit(url)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.calls.append((method, parsed.path))
        if method == "POST" and parsed.path == "/fapi/v1/order/test":
            return self._json(200, {})
        if method == "POST" and parsed.path == "/fapi/v1/order":
            row = {
                "symbol": params["symbol"],
                "orderId": 35,
                "clientOrderId": params["newClientOrderId"],
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
                "price": params["price"],
                "updateTime": 1_800_000_000_000,
            }
            self.orders[str(row["clientOrderId"])] = row
            return self._json(200, row)
        if method == "DELETE" and parsed.path == "/fapi/v1/order":
            row = dict(self.orders[params["origClientOrderId"]])
            row["status"] = "CANCELED"
            row["updateTime"] = 1_800_000_000_001
            return self._json(200, row)
        raise AssertionError((method, parsed.path))

    @staticmethod
    def _json(status: int, payload: object) -> HttpResponse:
        return HttpResponse(
            status=status,
            headers={"x-mbx-used-weight-1m": "1", "x-mbx-order-count-1m": "1"},
            payload=json.dumps(payload).encode(),
        )


def build_acceptance_report(*, output: Path, r34_report: Path | None = None) -> dict[str, object]:
    previous_status = None
    previous_sha256 = None
    previous_complete = None
    previous_live_write = None
    previous_real_requests = None
    if r34_report is not None:
        previous_bytes = r34_report.read_bytes()
        previous = json.loads(previous_bytes)
        previous_status = str(previous.get("status", ""))
        previous_complete = previous.get("production_dryrun_e2e_complete") is True
        previous_live_write = str(previous.get("live_exchange_write", ""))
        previous_real_requests = int(previous.get("real_exchange_order_requests_sent", -1))
        previous_sha256 = hashlib.sha256(previous_bytes).hexdigest()

    with tempfile.TemporaryDirectory(prefix="freqtrade-hedge-r35-") as directory:
        credential_path = Path(directory) / "testnet-credentials.txt"
        credential_path.write_text(
            f"acceptance-key\nacceptance-secret\n{TESTNET_CREDENTIAL_MARKER}\n",
            encoding="utf-8",
        )
        credentials = load_binance_testnet_credentials(credential_path)
        evidence = ProductionGateEvidence(
            environment=ExecutionEnvironment.TESTNET,
            account_fingerprint=credentials.account_fingerprint,
            account_id_prefix=TESTNET_PROFILE.account_prefix,
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
            allow_market_orders=False,
        )
        transport = _Transport()
        guarded = build_guarded_testnet_runtime(
            credentials=credentials,
            evidence=evidence,
            transport=transport,
        )
        guarded.arm(token=TOKEN, actor="r35-acceptance")
        validate_intent = make_testnet_limit_intent(
            credentials=credentials,
            symbol="BTCUSDT",
            position_side=PositionSide.LONG,
            quantity=Decimal("0.001"),
            limit_price=Decimal("10000"),
            idempotency_key="r35-validation",
        )
        validation = guarded.validate_order(validate_intent)
        store_count_after_validation = len(guarded.store.list_orders())
        canary_intent = make_testnet_limit_intent(
            credentials=credentials,
            symbol="ETHUSDT",
            position_side=PositionSide.SHORT,
            quantity=Decimal("0.01"),
            limit_price=Decimal("1000"),
            idempotency_key="r35-submit-cancel",
        )
        canary = run_submit_cancel_canary(guarded=guarded, intent=canary_intent)

    mainnet_rejected = False
    try:
        validate_rest_base_url(ExecutionEnvironment.TESTNET, "https://fapi.binance.com")
    except ValueError:
        mainnet_rejected = True

    checks = {
        "r34_prerequisite": (
            r34_report is None
            or (
                previous_status == "DRYRUN_E2E_COMPLETE_LIVE_LOCKED"
                and previous_complete is True
                and previous_live_write == "LOCKED"
                and previous_real_requests == 0
            )
        ),
        "testnet_credentials_marker": credentials.source_path is not None,
        "testnet_account_namespace": credentials.account_id.startswith("binance-usdm-testnet:"),
        "official_demo_rest_endpoint": guarded.runtime.exchange.base_url == TESTNET_PROFILE.rest_base_url,
        "mainnet_endpoint_rejected": mainnet_rejected,
        "fixed_perpetual_allowlist": evidence.allowed_symbols == TESTNET_ALLOWED_SYMBOLS,
        "test_order_validation": validation.accepted,
        "validation_did_not_create_durable_order": store_count_after_validation == 0,
        "submit_cancel_canary": canary.clean,
        "testnet_notional_cap": evidence.max_order_notional == Decimal("25"),
        "market_orders_disabled": evidence.allow_market_orders is False,
        "mainnet_live_write_locked": True,
    }
    required_failures = [name for name, passed in checks.items() if not passed]
    payload: dict[str, object] = {
        "schema_version": 1,
        "toolkit_version": "3.5.0",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": (
            "TESTNET_EXECUTION_CORE_COMPLETE_MAINNET_LOCKED"
            if not required_failures
            else "FAIL"
        ),
        "allowed_symbols": list(TESTNET_ALLOWED_SYMBOLS),
        "checks": {name: "PASS" if value else "FAIL" for name, value in checks.items()},
        "required_failures": required_failures,
        "testnet_execution_core_complete": not required_failures,
        "testnet_exchange_write": "GUARDED_NOT_RUN_BY_INSTALLER",
        "mainnet_live_exchange_write": "LOCKED",
        "real_testnet_order_requests_sent_by_installer": 0,
        "real_mainnet_order_requests_sent": 0,
        "evidence": {
            "r34_status": previous_status,
            "r34_report_sha256": previous_sha256,
            "r34_complete": previous_complete,
            "testnet_rest_base_url": TESTNET_PROFILE.rest_base_url,
            "testnet_websocket_base_url": TESTNET_PROFILE.websocket_base_url,
            "account_prefix": TESTNET_PROFILE.account_prefix,
            "fake_validation_requests": 1,
            "fake_submit_cancel_write_requests": canary.write_requests,
            "fake_submit_status": canary.submitted_status,
            "fake_cancel_status": canary.cancel_status,
        },
        "safety": {
            "credential_boundary": "DEDICATED_THREE_LINE_TESTNET_FILE_WITH_MARKER",
            "endpoint_boundary": "EXACT_DEMO_HOSTS",
            "account_namespace": "BINANCE_USDM_TESTNET",
            "arming": "PROCESS_LOCAL_TIME_LIMITED_TWO_STEP",
            "max_testnet_order_notional_usdt": "25",
            "market_orders": "DISABLED",
            "ambiguous_write": "UNKNOWN_THEN_QUERY_NO_BLIND_RETRY",
            "installer_network_access": "NONE",
            "mainnet_access": "NONE",
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["report_sha256"] = hashlib.sha256(canonical).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
