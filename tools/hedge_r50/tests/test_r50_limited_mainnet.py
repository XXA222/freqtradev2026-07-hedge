from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from freqtrade.hedge.execution.binance_usdm_adapter import BinanceExecutionTelemetry
from freqtrade.hedge.execution.service import IntentAction, PositionSide
from freqtrade.hedge.execution.state_machine import OrderState
from freqtrade.hedge.r5.arming import R5StageArmAuthority
from freqtrade.hedge.r5.budget import R5DailyBudgetStore
from freqtrade.hedge.r5.config import config_from_mapping
from freqtrade.hedge.r5.credentials import MAINNET_CREDENTIAL_MARKER, load_mainnet_credentials
from freqtrade.hedge.r5.evidence import R5EvidenceBundle
from freqtrade.hedge.r5.journal import R5Journal
from freqtrade.hedge.r5.mainnet_api import R5LivePreflight
from freqtrade.hedge.r5.market import R5Book, R5SymbolRules, quantity_for_notional
from freqtrade.hedge.r5.models import R5AccountSnapshot, R5RunState, R5Stage, default_limit_profile
from freqtrade.hedge.r5.orchestrator import R5MainnetOrchestrator, R5RunRequest
from freqtrade.hedge.r5.promotion import create_promotion_manifest, verify_promotion_manifest


OPERATOR = "operator-r5-token-123456789"
RISK = "independent-risk-token-987654321"
PROMOTION = "promotion-authority-token-111222333"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_config(tmp_path: Path):
    operator = {stage.value: digest(OPERATOR) for stage in R5Stage if stage.performs_exchange_write}
    risk = {stage.value: digest(RISK) for stage in R5Stage if stage.can_create_position}
    return config_from_mapping(
        {
            "hedge_r5": {
                "arm_token_sha256": operator,
                "risk_token_sha256": risk,
                "promotion_token_sha256": digest(PROMOTION),
                "journal_path": str(tmp_path / "journal.sqlite"),
                "budget_path": str(tmp_path / "budget.sqlite"),
            }
        },
        project_root=tmp_path,
    )


class FakeReadApi:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.long = Decimal("0")
        self.short = Decimal("0")
        self.open_orders = 0
        self.requests = 0
        self.rules_map = {
            "BTCUSDT": R5SymbolRules("BTCUSDT", "0.1", "0.001", "0.001", "100", "5"),
            "ETHUSDT": R5SymbolRules("ETHUSDT", "0.01", "0.001", "0.001", "1000", "5"),
        }
        self.books_map = {
            "BTCUSDT": R5Book("BTCUSDT", "60000", "60000.1", "60000.05"),
            "ETHUSDT": R5Book("ETHUSDT", "3000", "3000.01", "3000.005"),
        }

    def snapshot(self):
        return R5AccountSnapshot(
            self.account_id, "1000", self.long, self.short, self.open_orders, (), datetime.now(UTC)
        )

    def preflight(self):
        self.requests += 1
        return R5LivePreflight(
            "FULL_PASS", True, True, 100, True, True, True,
            ("BTCUSDT", "ETHUSDT"), self.snapshot(), self.rules_map, self.books_map, self.requests,
        )

    def account_snapshot(self):
        self.requests += 1
        return self.snapshot()


class FakeExecutor:
    def __init__(self, api: FakeReadApi):
        self.api = api
        self.writes = 0
        self.reads = 0
        self.orders = {}

    def validate(self, intent):
        self.writes += 1
        return SimpleNamespace(accepted=True, client_order_id="FTH-TEST")

    def submit(self, intent):
        self.writes += 1
        qty = intent.quantity
        price = intent.limit_price
        tif = intent.metadata["time_in_force"]
        if tif == "GTX":
            status = OrderState.ACKNOWLEDGED
            filled = Decimal("0")
            self.api.open_orders += 1
        else:
            status = OrderState.FILLED
            filled = qty
            notional = qty * price
            if intent.action in {IntentAction.OPEN, IntentAction.INCREASE}:
                if intent.position_side is PositionSide.LONG:
                    self.api.long += notional
                else:
                    self.api.short += notional
            else:
                if intent.position_side is PositionSide.LONG:
                    self.api.long = Decimal("0")
                else:
                    self.api.short = Decimal("0")
        client_id = f"FTH-{len(self.orders)+1}"
        lifecycle = SimpleNamespace(status=status, filled_quantity=filled, average_price=price if filled else None)
        order = SimpleNamespace(client_order_id=client_id, lifecycle=lifecycle, intent=intent)
        result = SimpleNamespace(order=order, idempotent_replay=False)
        self.orders[client_id] = result
        return result

    def cancel(self, client_order_id):
        self.writes += 1
        self.api.open_orders = max(0, self.api.open_orders - 1)
        original = self.orders[client_order_id]
        lifecycle = SimpleNamespace(status=OrderState.CANCELED, filled_quantity=Decimal("0"), average_price=None)
        order = SimpleNamespace(client_order_id=client_order_id, lifecycle=lifecycle, intent=original.order.intent)
        return SimpleNamespace(order=order, idempotent_replay=False)

    def query(self, client_order_id):
        self.reads += 1
        return self.orders.get(client_order_id)

    def telemetry(self):
        return BinanceExecutionTelemetry(self.writes + self.reads, self.writes, self.reads, 0, 0, 200, 10, 2)

    def disarm(self):
        return None


def make_orchestrator(tmp_path: Path):
    config = make_config(tmp_path)
    credential_file = tmp_path / "mainnet.txt"
    credential_file.write_text(f"key\nsecret\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    credentials = load_mainnet_credentials(credential_file)
    api = FakeReadApi(credentials.account_id)
    evidence = R5EvidenceBundle(tmp_path / "r4.json", tmp_path / "stream.json", "a" * 64, "b" * 64, "FULL_PASS", datetime.now(UTC))
    journal = R5Journal(config.journal_path)
    budget = R5DailyBudgetStore(config.budget_path)
    executors = []

    def factory(profile, gate, token, actor):
        item = FakeExecutor(api)
        executors.append(item)
        return item

    def auto_factory(stage, profile, executor, permit):
        def cycle(index, snapshot):
            return {"writes_attempted": 0, "index": index}
        return cycle

    return config, credentials, api, journal, budget, R5MainnetOrchestrator(
        config=config, credentials=credentials, evidence=evidence, read_api=api,
        journal=journal, budget=budget, executor_factory=factory, auto_cycle_factory=auto_factory,
    )


def request(stage, credentials, run_id=None):
    run_id = run_id or stage.value.lower()
    return R5RunRequest(
        stage, run_id, "tester", "ETHUSDT", PositionSide.LONG, Decimal("10"),
        None if stage is R5Stage.MAINNET_READONLY else OPERATOR,
        RISK if stage.can_create_position else None,
        None if stage is R5Stage.MAINNET_READONLY else f"R5:{stage.value}:{run_id}:{credentials.account_fingerprint}",
        2,
    )


def test_credentials_require_dedicated_marker(tmp_path):
    path = tmp_path / "key.txt"
    path.write_text("key\nsecret\nWRONG\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_mainnet_credentials(path)


def test_stage_profiles_are_monotonic_and_hard_capped():
    profiles = [default_limit_profile(stage) for stage in R5Stage]
    assert profiles[-1].max_single_order_notional <= Decimal("250")
    assert profiles[-1].max_gross_notional <= Decimal("1000")
    assert profiles[0].write_request_budget == 0
    assert profiles[1].write_request_budget == 1


def test_stage_arm_requires_distinct_two_party_tokens(tmp_path):
    config = make_config(tmp_path)
    authority = R5StageArmAuthority(operator_hashes=dict(config.operator_token_sha256), risk_hashes=dict(config.risk_token_sha256))
    profile = config.profiles[R5Stage.SINGLE_LEG]
    with pytest.raises(PermissionError):
        authority.issue(
            stage=R5Stage.SINGLE_LEG, run_id="x", account_fingerprint="abc", actor="a",
            operator_token=OPERATOR, risk_token=OPERATOR,
            confirmation="R5:SINGLE_LEG:x:abc", profile=profile,
        )


def test_quantity_rejects_minimum_above_stage_cap():
    rules = R5SymbolRules("BTCUSDT", "0.1", "0.001", "0.001", "100", "5")
    with pytest.raises(ValueError):
        quantity_for_notional(rules=rules, price=Decimal("60000"), target_notional=Decimal("10"), maximum_notional=Decimal("10"))


def test_all_seven_stages_run_sequentially_and_replay_zero_action(tmp_path):
    config, credentials, api, journal, budget, orchestrator = make_orchestrator(tmp_path)
    reports = {}
    for stage in list(R5Stage)[:-1]:
        report = orchestrator.run(request(stage, credentials))
        assert report["status"] == "PASS", report
        reports[stage] = report
    restricted = reports[R5Stage.RESTRICTED_AUTO]
    manifest = create_promotion_manifest(
        account_id=credentials.account_id,
        previous_report=restricted,
        profile=config.profiles[R5Stage.EXPANDED_LIMITS],
        promotion_token=PROMOTION,
    )
    expanded_request = request(R5Stage.EXPANDED_LIMITS, credentials)
    expanded = orchestrator.run(expanded_request, promotion_manifest=manifest, promotion_token=PROMOTION)
    assert expanded["status"] == "PASS"
    replay = orchestrator.run(expanded_request, promotion_manifest=manifest, promotion_token=PROMOTION)
    assert replay == expanded
    assert api.snapshot().flat
    journal.close()
    budget.close()


def test_cannot_skip_stage(tmp_path):
    _, credentials, _, journal, budget, orchestrator = make_orchestrator(tmp_path)
    report = orchestrator.run(request(R5Stage.SINGLE_LEG, credentials))
    assert report["state"] == R5RunState.FAILED.value
    assert "requires completed" in report["error"]
    journal.close(); budget.close()


def test_interrupted_executing_run_never_resubmits(tmp_path):
    config, credentials, _, journal, budget, orchestrator = make_orchestrator(tmp_path)
    req = request(R5Stage.MAINNET_READONLY, credentials, "interrupted")
    journal.claim(run_id=req.run_id, stage=req.stage, request_hash=req.request_hash, account_id=credentials.account_id, actor=req.actor, config=req.canonical_payload())
    journal.transition(run_id=req.run_id, target=R5RunState.PREFLIGHT, event="x")
    journal.transition(run_id=req.run_id, target=R5RunState.EXECUTING, event="x")
    report = orchestrator.run(req)
    assert report["state"] == R5RunState.RECOVERY_REQUIRED.value
    assert report["checks"]["blind_resubmission_blocked"] == "PASS"
    journal.close(); budget.close()


def test_promotion_is_account_profile_and_token_bound(tmp_path):
    config = make_config(tmp_path)
    previous = {"status": "PASS", "stage": "RESTRICTED_AUTO"}
    profile = config.profiles[R5Stage.EXPANDED_LIMITS]
    manifest = create_promotion_manifest(account_id="binance-usdm:abc", previous_report=previous, profile=profile, promotion_token=PROMOTION)
    verify_promotion_manifest(manifest, expected_token_sha256=digest(PROMOTION), promotion_token=PROMOTION, account_id="binance-usdm:abc", previous_report=previous, profile=profile)
    with pytest.raises(PermissionError):
        verify_promotion_manifest(manifest, expected_token_sha256=digest(PROMOTION), promotion_token="wrong", account_id="binance-usdm:abc", previous_report=previous, profile=profile)


def test_sqlite_files_release_on_windows_semantics(tmp_path):
    journal_path = tmp_path / "journal.sqlite"
    budget_path = tmp_path / "budget.sqlite"
    with R5Journal(journal_path):
        pass
    with R5DailyBudgetStore(budget_path):
        pass
    journal_path.unlink()
    budget_path.unlink()


def test_binance_adapter_sends_gtx_and_self_trade_prevention():
    from freqtrade.hedge.execution.binance_usdm_adapter import (
        BinanceExecutionCredentials,
        BinanceUSDMExecutionAdapter,
        HttpResponse,
    )
    from freqtrade.hedge.execution.production_gate import (
        ExecutionEnvironment,
        ProductionExecutionGate,
        ProductionGateEvidence,
    )
    from freqtrade.hedge.execution.service import ApprovedOrderIntent, InMemoryExecutionStore, OrderIntent, OrderType
    from freqtrade.hedge.execution.client_order_id import build_client_order_id

    class Transport:
        def __init__(self):
            self.urls = []
        def request(self, method, url, headers, timeout):
            self.urls.append((method, url, dict(headers)))
            if "/time" in url:
                return HttpResponse(200, {}, b'{"serverTime":1000}')
            return HttpResponse(200, {}, b'{}')

    transport = Transport()
    evidence = ProductionGateEvidence(
        environment=ExecutionEnvironment.LIVE,
        account_fingerprint="abc",
        allowed_symbols=("ETHUSDT",),
        cross_margin_symbols=("ETHUSDT",),
        readonly_status="FULL_PASS",
        user_stream_status="FULL_PASS",
        hedge_mode_enabled=True,
        clock_offset_ms=0,
        live_trading_enabled=True,
        strict_key_policy_passed=True,
        futures_trading_permission=True,
        ip_restricted=True,
        expected_arm_token_sha256=digest(OPERATOR),
        max_order_notional=Decimal("10"),
        allow_market_orders=False,
    )
    gate = ProductionExecutionGate(evidence)
    gate.arm(token=OPERATOR, actor="test", confirmed=True, ttl_seconds=30)
    store = InMemoryExecutionStore()
    adapter = BinanceUSDMExecutionAdapter(
        credentials=BinanceExecutionCredentials("key", "secret"),
        gate=gate,
        store=store,
        base_url="https://fapi.binance.com",
        transport=transport,
        now_ms=lambda: 1000,
    )
    intent = OrderIntent(
        account_id="binance-usdm:abc",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        action=IntentAction.OPEN,
        quantity=Decimal("0.003"),
        idempotency_key="stp",
        order_type=OrderType.LIMIT,
        limit_price=Decimal("3000"),
        metadata={"time_in_force": "GTX", "self_trade_prevention_mode": "EXPIRE_BOTH"},
    )
    client_id = build_client_order_id(
        account_id=intent.account_id,
        symbol=intent.symbol,
        position_side=intent.position_side.value,
        idempotency_key=intent.idempotency_key,
    )
    approved = ApprovedOrderIntent(intent, intent.quantity, client_id, datetime.now(UTC), ("PASS",))
    result = adapter.validate_order(approved)
    assert result.accepted is True
    url = transport.urls[-1][1]
    assert "timeInForce=GTX" in url
    assert "selfTradePreventionMode=EXPIRE_BOTH" in url
    assert "X-MBX-APIKEY" in transport.urls[-1][2]


def test_mainnet_read_api_rejects_non_allowlisted_endpoint(tmp_path):
    from freqtrade.hedge.r5.mainnet_api import R5MainnetReadApi
    credential_file = tmp_path / "mainnet.txt"
    credential_file.write_text(f"key\nsecret\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    api = R5MainnetReadApi(credentials=load_mainnet_credentials(credential_file), transport=SimpleNamespace())
    with pytest.raises(PermissionError):
        api._perform("https://fapi.binance.com", "/fapi/v1/leverage", {}, authenticated=True)


def test_mainnet_readonly_can_refresh_without_any_executor_write(tmp_path):
    _, credentials, _, journal, budget, orchestrator = make_orchestrator(tmp_path)
    first = orchestrator.run(request(R5Stage.MAINNET_READONLY, credentials, "readonly-first"))
    assert first["status"] == "PASS"
    second = orchestrator.run(request(R5Stage.MAINNET_READONLY, credentials, "readonly-second"))
    assert second["status"] == "PASS"
    assert second["writes_attempted"] == 0
    assert second["real_mainnet_order_requests_sent"] == 0
    journal.close(); budget.close()


def test_completion_and_stage_evidence_are_committed_together(tmp_path):
    config = make_config(tmp_path)
    credential_file = tmp_path / "mainnet.txt"
    credential_file.write_text(f"key\nsecret\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    credentials = load_mainnet_credentials(credential_file)
    with R5Journal(config.journal_path) as journal:
        record, _ = journal.claim(
            run_id="atomic", stage=R5Stage.MAINNET_READONLY, request_hash="f" * 64,
            account_id=credentials.account_id, actor="tester", config={},
        )
        journal.transition(run_id=record.run_id, target=R5RunState.PREFLIGHT, event="x")
        journal.transition(run_id=record.run_id, target=R5RunState.EXECUTING, event="x")
        journal.transition(run_id=record.run_id, target=R5RunState.VERIFYING, event="x")
        completed, digest_value = journal.complete_with_evidence(
            run_id=record.run_id, report={"status": "PASS", "run_id": "atomic"}
        )
        assert completed.state is R5RunState.COMPLETED
        assert len(digest_value) == 64
        assert journal.stage_evidence(account_id=credentials.account_id, stage=R5Stage.MAINNET_READONLY)["status"] == "PASS"
