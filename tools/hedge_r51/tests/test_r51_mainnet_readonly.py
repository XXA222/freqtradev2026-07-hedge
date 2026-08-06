from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from freqtrade.hedge.execution.binance_usdm_adapter import HttpResponse
from freqtrade.hedge.r5.budget import R5DailyBudgetStore
from freqtrade.hedge.r5.config import DISABLED_TOKEN_SHA256, load_config
from freqtrade.hedge.r5.credentials import (
    MAINNET_CREDENTIAL_MARKER,
    R24_LEGACY_MISSING_ACCOUNT_ALIAS,
    load_mainnet_credentials,
)
from freqtrade.hedge.r5.evidence import load_evidence_bundle
from freqtrade.hedge.r5.journal import R5Journal
from freqtrade.hedge.r5.mainnet_api import R5LivePreflight, R5MainnetReadApi
from freqtrade.hedge.r5.market import R5Book, R5SymbolRules
from freqtrade.hedge.r5.models import R5AccountSnapshot, R5RunState, R5Stage
from freqtrade.hedge.r5.orchestrator import R5MainnetOrchestrator, R5RunRequest
from freqtrade.hedge.r5.readonly_bootstrap import prepare_readonly_workspace
from tools.hedge_r51.r51_toolkit.acceptance import build_acceptance_report


R24_FP = "af163dd122b5c16a57d4e45c"


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _r4(path: Path) -> Path:
    return _write_json(path, {
        "status": "R4_FULL_COMPLETION_COMPLETE_MAINNET_LOCKED",
        "mainnet_live_exchange_write": "LOCKED",
        "real_mainnet_order_requests_sent": 0,
    })


def _r24(path: Path, fingerprint: str = R24_FP) -> Path:
    return _write_json(path, {
        "status": "FULL_PASS",
        "account_fingerprint": fingerprint,
        "security": {"exchange_writes": "LOCKED"},
        "gate_domains": {
            "clock_readiness": "PASS",
            "authenticated_read_access": "PASS",
            "account_and_hedge_semantics": "PASS",
            "database_persistence": "PASS",
            "history_completeness": "PASS",
            "money_movement_safety": "PASS",
        },
    })


def _stream(path: Path, *, fingerprint: str = R24_FP, when: datetime | None = None) -> Path:
    observed = when or datetime.now(UTC)
    return _write_json(path, {
        "status": "PASS_WITH_NO_USER_EVENTS",
        "completed_at_utc": observed.isoformat(),
        "result": {"final_state": "READY", "ready_observed": True},
        "direction2_health": {
            "clock_synchronized": True,
            "configuration_valid": True,
            "reconciliation_consistent": True,
            "rest_fresh": True,
            "stream_connected": True,
            "stream_fresh": True,
            "unmanaged_order_count": 0,
            "unmanaged_position_count": 0,
            "last_stream_event_at": None,
        },
        "security": {
            "exchange_order_writes": "LOCKED",
            "trading_endpoints_allowed": False,
            "credentials_persisted": False,
            "write_lock": {"enabled": True},
        },
        "database": {
            "reopen_readback": "PASS",
            "integrity_before_reopen": {"status": "PASS"},
            "integrity_after_reopen": {"status": "PASS"},
        },
        "scope": {
            "account_fingerprint": fingerprint,
            "symbols": ["BTCUSDT", "ETHUSDT"],
        },
        "stream": {"last_event_at": None},
    })


def _config(tmp_path: Path, credentials: Path, r4: Path, r24: Path, stream: Path) -> Path:
    return _write_json(tmp_path / "config.json", {"hedge_r5": {
        "credentials_path": str(credentials),
        "journal_path": str(tmp_path / "journal.sqlite"),
        "budget_path": str(tmp_path / "budget.sqlite"),
        "r4_report": str(r4),
        "r24_report": str(r24),
        "user_stream_report": str(stream),
    }})


class FakeReadApi:
    def __init__(
        self,
        account_id: str,
        *,
        evidence_fingerprint: str = R24_FP,
        futures_permission: bool = True,
        ip_restricted: bool = True,
        withdrawals_disabled: bool = True,
        reading_permission: bool = True,
        can_trade: bool = True,
        long_notional: str = "0",
        short_notional: str = "0",
        open_orders: int = 0,
    ) -> None:
        self.account = R5AccountSnapshot(
            account_id, "1000", long_notional, short_notional, open_orders, (), datetime.now(UTC)
        )
        self.evidence_fingerprint = evidence_fingerprint
        self.futures_permission = futures_permission
        self.ip_restricted = ip_restricted
        self.withdrawals_disabled = withdrawals_disabled
        self.reading_permission = reading_permission
        self.can_trade = can_trade
        self.rules = {
            "BTCUSDT": R5SymbolRules("BTCUSDT", "0.1", "0.001", "0.001", "100", "5"),
            "ETHUSDT": R5SymbolRules("ETHUSDT", "0.01", "0.001", "0.001", "1000", "5"),
        }
        self.books = {
            "BTCUSDT": R5Book("BTCUSDT", "60000", "60000.1", "60000.05"),
            "ETHUSDT": R5Book("ETHUSDT", "3000", "3000.01", "3000.005"),
        }

    def preflight(self) -> R5LivePreflight:
        return R5LivePreflight(
            readonly_status="FULL_PASS",
            hedge_mode_enabled=True,
            single_asset_mode=True,
            clock_offset_ms=25,
            futures_trading_permission=self.futures_permission,
            ip_restricted=self.ip_restricted,
            withdrawals_disabled=self.withdrawals_disabled,
            cross_margin_symbols=("BTCUSDT", "ETHUSDT"),
            account=self.account,
            rules=self.rules,
            books=self.books,
            request_count=12,
            account_can_trade=self.can_trade,
            api_reading_permission=self.reading_permission,
            account_alias="Sg123456789",
            evidence_account_fingerprint=self.evidence_fingerprint,
            evidence_account_fingerprint_candidates=(self.evidence_fingerprint,),
            evidence_account_fingerprint_modes=("FAKE_TEST_BINDING",),
            account_alias_present=True,
            managed_position_legs=(),
            active_order_summaries=(),
            balance_assets=("USDT",),
        )

    def account_snapshot(self) -> R5AccountSnapshot:
        return self.account


def _orchestrator(tmp_path: Path, *, api: FakeReadApi | None = None):
    key, secret = "K" * 32, "S" * 32
    credentials_path = tmp_path / "credentials.txt"
    credentials_path.write_text(f"{key}\n{secret}\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    r4, r24, stream = _r4(tmp_path / "r4.json"), _r24(tmp_path / "r24.json"), _stream(tmp_path / "stream.json")
    config = load_config(_config(tmp_path, credentials_path, r4, r24, stream), project_root=tmp_path, allow_unarmed_readonly=True)
    credentials = load_mainnet_credentials(credentials_path)
    evidence = load_evidence_bundle(r4_report=r4, r24_report=r24, user_stream_report=stream, max_age_seconds=900)
    read_api = api or FakeReadApi(credentials.account_id)
    journal = R5Journal(config.journal_path)
    budget = R5DailyBudgetStore(config.budget_path)
    orchestrator = R5MainnetOrchestrator(
        config=config,
        credentials=credentials,
        evidence=evidence,
        read_api=read_api,
        journal=journal,
        budget=budget,
        executor_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("readonly must not build executor")),
    )
    return config, credentials, journal, budget, orchestrator


def test_user_stream_evidence_accepts_completed_at_utc_and_null_event(tmp_path: Path):
    r4, r24 = _r4(tmp_path / "r4.json"), _r24(tmp_path / "r24.json")
    completed = datetime.now(UTC)
    stream = _stream(tmp_path / "stream.json", when=completed)
    evidence = load_evidence_bundle(r4_report=r4, r24_report=r24, user_stream_report=stream, max_age_seconds=900)
    assert evidence.user_stream_created_at == completed


def test_evidence_rejects_naive_stale_future_and_account_mismatch(tmp_path: Path):
    r4, r24 = _r4(tmp_path / "r4.json"), _r24(tmp_path / "r24.json")
    naive = json.loads(_stream(tmp_path / "naive.json").read_text())
    naive["completed_at_utc"] = "2026-08-04T12:00:00"
    _write_json(tmp_path / "naive.json", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        load_evidence_bundle(r4_report=r4, r24_report=r24, user_stream_report=tmp_path / "naive.json", max_age_seconds=900)
    with pytest.raises(PermissionError, match="stale"):
        load_evidence_bundle(r4_report=r4, r24_report=r24, user_stream_report=_stream(tmp_path / "stale.json", when=datetime.now(UTC)-timedelta(hours=1)), max_age_seconds=900)
    with pytest.raises(PermissionError, match="different accounts"):
        load_evidence_bundle(r4_report=r4, r24_report=r24, user_stream_report=_stream(tmp_path / "mismatch.json", fingerprint="different"), max_age_seconds=900)


def test_readonly_config_can_be_unarmed_but_write_config_cannot(tmp_path: Path):
    path = _write_json(tmp_path / "config.json", {"hedge_r5": {
        "credentials_path": str(tmp_path / "credentials.txt"),
        "journal_path": str(tmp_path / "journal.sqlite"),
        "budget_path": str(tmp_path / "budget.sqlite"),
        "r4_report": str(tmp_path / "r4.json"),
        "r24_report": str(tmp_path / "r24.json"),
        "user_stream_report": str(tmp_path / "stream.json"),
    }})
    config = load_config(path, project_root=tmp_path, allow_unarmed_readonly=True)
    assert set(config.operator_token_sha256.values()) == {DISABLED_TOKEN_SHA256}
    assert set(config.risk_token_sha256.values()) == {DISABLED_TOKEN_SHA256}
    with pytest.raises(ValueError, match="SHA256"):
        load_config(path, project_root=tmp_path, allow_unarmed_readonly=False)


def test_bootstrap_uses_original_credentials_and_removes_only_exact_legacy_copy(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _r24(project / "artifacts/r24/report.json")
    _r4(project / "artifacts/r4/report.json")
    key, secret = "K" * 32, "S" * 32
    legacy = tmp_path / "legacy.txt"
    legacy.write_text(f"{key}\n{secret}\n", encoding="utf-8")
    stale = project / "user_data/r5/binance-mainnet-r5.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text(f"{key}\n{secret}\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    workspace = prepare_readonly_workspace(project_root=project, legacy_credentials=legacy, output_dir=project / "artifacts/r51")
    assert workspace.credentials_path == legacy.resolve()
    assert workspace.removed_generated_credential_copy is True
    assert not stale.exists()
    serialized = json.dumps(workspace.to_dict()) + workspace.config_path.read_text(encoding="ascii")
    assert key not in serialized and secret not in serialized
    assert workspace.r24_account_fingerprint == R24_FP


def test_readonly_observation_passes_with_disabled_trade_policy_and_managed_exposure(tmp_path: Path):
    key = "K" * 32
    account_id = f"binance-usdm:{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    api = FakeReadApi(
        account_id,
        futures_permission=False,
        ip_restricted=False,
        can_trade=False,
        long_notional="25",
        short_notional="20",
        open_orders=5,
    )
    _, _, journal, budget, orchestrator = _orchestrator(tmp_path, api=api)
    try:
        report = orchestrator.run(R5RunRequest(stage=R5Stage.MAINNET_READONLY, run_id="readonly-key-policy", actor="operator"))
    finally:
        journal.close(); budget.close()
    assert report["status"] == "PASS"
    assert report["writes_attempted"] == 0
    assert report["evidence"]["order_test_promotion_ready"] is False
    assert report["evidence"]["order_test_promotion_block_reasons"] == [
        "FUTURES_TRADING_PERMISSION_DISABLED",
        "IP_RESTRICTION_DISABLED",
        "ACCOUNT_TRADING_DISABLED",
        "ACCOUNT_NOT_FLAT",
        "ACTIVE_ORDERS_PRESENT",
    ]


def test_live_account_fingerprint_mismatch_is_hard_failure(tmp_path: Path):
    key = "K" * 32
    account_id = f"binance-usdm:{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    api = FakeReadApi(account_id, evidence_fingerprint="wrong-account")
    _, _, journal, budget, orchestrator = _orchestrator(tmp_path, api=api)
    try:
        report = orchestrator.run(R5RunRequest(stage=R5Stage.MAINNET_READONLY, run_id="mismatch", actor="operator"))
    finally:
        journal.close(); budget.close()
    assert report["status"] == "FAILED"
    assert "does not match" in report["error"]


def test_readonly_evidence_refreshes_and_old_run_replay_remains_consistent(tmp_path: Path):
    journal = R5Journal(tmp_path / "journal.sqlite")
    try:
        def complete(run_id: str, ready: bool):
            journal.claim(run_id=run_id, stage=R5Stage.MAINNET_READONLY, request_hash=run_id, account_id="binance-usdm:acct", actor="operator", config={})
            journal.transition(run_id=run_id, target=R5RunState.PREFLIGHT, event="P")
            journal.transition(run_id=run_id, target=R5RunState.EXECUTING, event="E")
            journal.transition(run_id=run_id, target=R5RunState.VERIFYING, event="V")
            report = {"run_id": run_id, "evidence": {"order_test_promotion_ready": ready}}
            return report, journal.complete_with_evidence(run_id=run_id, report=report, replace_stage_evidence=True)[1]
        first, first_hash = complete("readonly-1", False)
        second, _ = complete("readonly-2", True)
        assert journal.stage_evidence(account_id="binance-usdm:acct", stage=R5Stage.MAINNET_READONLY) == second
        assert journal.complete_with_evidence(run_id="readonly-1", report=first, replace_stage_evidence=True)[1] == first_hash
    finally:
        journal.close()


def test_order_test_requires_latest_fresh_promotion_ready_readonly_evidence(tmp_path: Path):
    journal = R5Journal(tmp_path / "journal.sqlite")
    try:
        journal.claim(run_id="readonly", stage=R5Stage.MAINNET_READONLY, request_hash="x", account_id="binance-usdm:acct", actor="op", config={})
        for state in (R5RunState.PREFLIGHT, R5RunState.EXECUTING, R5RunState.VERIFYING):
            journal.transition(run_id="readonly", target=state, event=state.value)
        journal.complete_with_evidence(run_id="readonly", report={"evidence": {"order_test_promotion_ready": False}}, replace_stage_evidence=True)
        with pytest.raises(PermissionError, match="promotion readiness"):
            journal.assert_previous_complete(account_id="binance-usdm:acct", stage=R5Stage.ORDER_TEST, require_promotion_ready=True, max_age_seconds=900)
    finally:
        journal.close()


def test_signed_get_refreshes_timestamp_and_signature_on_retry(tmp_path: Path):
    class Transport:
        def __init__(self): self.urls = []
        def request(self, method, url, headers, timeout):
            self.urls.append(url)
            if len(self.urls) == 1:
                return HttpResponse(500, {}, b'{"code":-1,"msg":"temporary"}')
            return HttpResponse(200, {}, b'{"dualSidePosition":true}')
    counter = iter([1000, 2000, 3000, 4000])
    creds_file = tmp_path / "creds.txt"
    creds_file.write_text(f"{'K'*32}\n{'S'*32}\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8")
    transport = Transport()
    api = R5MainnetReadApi(credentials=load_mainnet_credentials(creds_file), transport=transport, now_ms=lambda: next(counter), sleep=lambda _: None)
    api.signed_get("https://fapi.binance.com", "/fapi/v1/positionSide/dual")
    first, second = [parse_qs(urlsplit(url).query) for url in transport.urls]
    assert first["timestamp"] != second["timestamp"]
    assert first["signature"] != second["signature"]


def test_acceptance_separates_complete_observation_from_key_and_account_blocks(tmp_path: Path):
    stage = _write_json(tmp_path / "stage.json", {
        "status": "PASS", "state": "COMPLETED", "stage": "MAINNET_READONLY",
        "account_id": "binance-usdm:abcdef1234567890", "writes_attempted": 0,
        "real_mainnet_order_requests_sent": 0,
        "evidence": {
            "readonly_requests": 12,
            "r24_account_fingerprint": R24_FP,
            "live_evidence_account_fingerprint": R24_FP,
            "live_evidence_binding_mode": "R24_LEGACY_MISSING_ACCOUNT_ALIAS",
            "order_test_promotion_ready": False,
            "order_test_promotion_block_reasons": ["FUTURES_TRADING_PERMISSION_DISABLED", "ACCOUNT_NOT_FLAT"],
        },
    })
    stream = _stream(tmp_path / "stream.json")
    workspace = _write_json(tmp_path / "workspace.json", {
        "account_fingerprint": "abcdef1234567890",
        "r24_account_fingerprint": R24_FP,
        "credentials_persisted_by_r51": False,
        "exchange_write": "LOCKED",
        "real_mainnet_order_requests_planned": 0,
    })
    payload = build_acceptance_report(output=tmp_path / "acceptance.json", stage_report=stage, stream_report=stream, workspace_manifest=workspace)
    assert payload["status"] == "R5_1_MAINNET_READONLY_COMPLETE_WRITES_LOCKED"
    assert payload["next_stage"] == "BLOCKED_KEY_AND_ACCOUNT_STATE"
    assert payload["required_failures"] == []



def test_missing_account_alias_reproduces_r24_legacy_unknown_fingerprint(tmp_path: Path):
    key, secret = "K" * 32, "S" * 32
    credentials_path = tmp_path / "credentials.txt"
    credentials_path.write_text(
        f"{key}\n{secret}\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8"
    )
    credentials = load_mainnet_credentials(credentials_path)
    expected = credentials.r24_account_fingerprint(R24_LEGACY_MISSING_ACCOUNT_ALIAS)
    assert credentials.r24_account_fingerprint("") == expected
    assert credentials.r24_account_fingerprint(None) == expected
    assert credentials.r24_account_fingerprint_candidates("") == (
        ("R24_LEGACY_MISSING_ACCOUNT_ALIAS", expected),
    )


def test_live_preflight_accepts_missing_account_alias_and_exposes_legacy_candidate(tmp_path: Path):
    key, secret = "K" * 32, "S" * 32
    credentials_path = tmp_path / "credentials.txt"
    credentials_path.write_text(
        f"{key}\n{secret}\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8"
    )
    credentials = load_mainnet_credentials(credentials_path)

    class Transport:
        def request(self, method, url, headers, timeout):
            path = urlsplit(url).path
            if path == "/fapi/v1/time":
                return HttpResponse(200, {}, b'{"serverTime":1000}')
            if path == "/fapi/v1/positionSide/dual":
                return HttpResponse(200, {}, b'{"dualSidePosition":true}')
            if path == "/fapi/v1/multiAssetsMargin":
                return HttpResponse(200, {}, b'{"multiAssetsMargin":false}')
            if path == "/sapi/v1/account/apiRestrictions":
                return HttpResponse(200, {}, b'{"enableReading":true,"enableFutures":false,"ipRestrict":false,"enableWithdrawals":false}')
            if path == "/fapi/v3/account":
                return HttpResponse(200, {}, b'{"canTrade":false,"totalMarginBalance":"1000","assets":[]}')
            if path == "/fapi/v3/positionRisk":
                return HttpResponse(200, {}, b'[]')
            if path == "/fapi/v1/openOrders":
                return HttpResponse(200, {}, b'[]')
            if path == "/fapi/v1/symbolConfig":
                symbol = parse_qs(urlsplit(url).query)["symbol"][0]
                return HttpResponse(200, {}, json.dumps([{"symbol":symbol,"marginType":"CROSSED"}]).encode())
            if path == "/fapi/v1/exchangeInfo":
                payload = {"symbols": [
                    {"symbol":"BTCUSDT","contractType":"PERPETUAL","status":"TRADING","filters":[
                        {"filterType":"PRICE_FILTER","tickSize":"0.1"},
                        {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
                        {"filterType":"MIN_NOTIONAL","notional":"5"}]},
                    {"symbol":"ETHUSDT","contractType":"PERPETUAL","status":"TRADING","filters":[
                        {"filterType":"PRICE_FILTER","tickSize":"0.01"},
                        {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"1000"},
                        {"filterType":"MIN_NOTIONAL","notional":"5"}]}]}
                return HttpResponse(200, {}, json.dumps(payload).encode())
            if path == "/fapi/v1/ticker/bookTicker":
                return HttpResponse(200, {}, b'{"bidPrice":"100","askPrice":"101"}')
            raise AssertionError(path)

    api = R5MainnetReadApi(
        credentials=credentials,
        transport=Transport(),
        now_ms=lambda: 1000,
        sleep=lambda _: None,
    )
    preflight = api.preflight()
    expected = credentials.r24_account_fingerprint(R24_LEGACY_MISSING_ACCOUNT_ALIAS)
    assert preflight.account_alias == ""
    assert preflight.account_alias_present is False
    assert preflight.evidence_account_fingerprint == expected
    assert preflight.evidence_account_fingerprint_candidates == (expected,)
    assert preflight.evidence_account_fingerprint_modes == (
        "R24_LEGACY_MISSING_ACCOUNT_ALIAS",
    )


def test_orchestrator_accepts_missing_alias_when_exact_r24_legacy_candidate_matches(tmp_path: Path):
    key, secret = "K" * 32, "S" * 32
    credentials_path = tmp_path / "credentials.txt"
    credentials_path.write_text(
        f"{key}\n{secret}\n{MAINNET_CREDENTIAL_MARKER}\n", encoding="utf-8"
    )
    credentials = load_mainnet_credentials(credentials_path)
    expected = credentials.r24_account_fingerprint(R24_LEGACY_MISSING_ACCOUNT_ALIAS)
    r4 = _r4(tmp_path / "r4.json")
    r24 = _r24(tmp_path / "r24.json", fingerprint=expected)
    stream = _stream(tmp_path / "stream.json", fingerprint=expected)
    config = load_config(
        _config(tmp_path, credentials_path, r4, r24, stream),
        project_root=tmp_path,
        allow_unarmed_readonly=True,
    )
    evidence = load_evidence_bundle(
        r4_report=r4, r24_report=r24, user_stream_report=stream, max_age_seconds=900
    )
    base = FakeReadApi(credentials.account_id, evidence_fingerprint=expected)

    class MissingAliasApi(FakeReadApi):
        def preflight(self) -> R5LivePreflight:
            value = base.preflight()
            return R5LivePreflight(
                readonly_status=value.readonly_status,
                hedge_mode_enabled=value.hedge_mode_enabled,
                single_asset_mode=value.single_asset_mode,
                clock_offset_ms=value.clock_offset_ms,
                futures_trading_permission=False,
                ip_restricted=False,
                withdrawals_disabled=True,
                cross_margin_symbols=value.cross_margin_symbols,
                account=value.account,
                rules=value.rules,
                books=value.books,
                request_count=value.request_count,
                account_can_trade=False,
                api_reading_permission=True,
                account_alias="",
                evidence_account_fingerprint=expected,
                evidence_account_fingerprint_candidates=(expected,),
                evidence_account_fingerprint_modes=(
                    "R24_LEGACY_MISSING_ACCOUNT_ALIAS",
                ),
                account_alias_present=False,
            )

    journal = R5Journal(config.journal_path)
    budget = R5DailyBudgetStore(config.budget_path)
    orchestrator = R5MainnetOrchestrator(
        config=config,
        credentials=credentials,
        evidence=evidence,
        read_api=MissingAliasApi(credentials.account_id),
        journal=journal,
        budget=budget,
        executor_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("readonly must not build executor")
        ),
    )
    try:
        report = orchestrator.run(
            R5RunRequest(
                stage=R5Stage.MAINNET_READONLY,
                run_id="missing-alias-legacy-binding",
                actor="operator",
            )
        )
    finally:
        journal.close()
        budget.close()
    assert report["status"] == "PASS"
    assert report["evidence"]["live_evidence_account_fingerprint"] == expected
    assert report["evidence"]["live_evidence_binding_mode"] == (
        "R24_LEGACY_MISSING_ACCOUNT_ALIAS"
    )
    assert report["evidence"]["account_alias_present"] is False
    assert report["writes_attempted"] == 0


def test_acceptance_rejects_stage_without_explicit_live_r24_binding(tmp_path: Path):
    stage = _write_json(tmp_path / "stage.json", {
        "status": "PASS", "state": "COMPLETED", "stage": "MAINNET_READONLY",
        "account_id": "binance-usdm:abcdef1234567890", "writes_attempted": 0,
        "real_mainnet_order_requests_sent": 0,
        "evidence": {
            "readonly_requests": 12,
            "r24_account_fingerprint": R24_FP,
            "order_test_promotion_ready": False,
            "order_test_promotion_block_reasons": ["ACCOUNT_NOT_FLAT"],
        },
    })
    workspace = _write_json(tmp_path / "workspace.json", {
        "account_fingerprint": "abcdef1234567890",
        "r24_account_fingerprint": R24_FP,
        "credentials_persisted_by_r51": False,
        "exchange_write": "LOCKED",
        "real_mainnet_order_requests_planned": 0,
    })
    payload = build_acceptance_report(
        output=tmp_path / "acceptance.json",
        stage_report=stage,
        stream_report=_stream(tmp_path / "stream.json"),
        workspace_manifest=workspace,
    )
    assert payload["mainnet_readonly_complete"] is False
    assert "live_r24_account_bound" in payload["required_failures"]


def test_online_runner_uses_separate_native_stream_capture_and_no_credential_acl_mutation():
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts/Run-Freqtrade-Hedge-R5.1-MainnetReadonly.ps1").read_text(encoding="ascii")
    assert "1> $StdoutCapture 2> $StderrCapture" in script
    assert "2>&1" not in script
    assert "icacls.exe $CredentialPath" not in script
    assert "$ExitCode = [int]$LASTEXITCODE" in script
    assert "return $ExitCode" in script


def test_offline_acceptance_proves_full_readonly_contract(tmp_path: Path):
    from tools.hedge_r51.r51_toolkit.offline_acceptance import build_report
    root = Path(__file__).resolve().parents[3]
    payload = build_report(project_root=root, output=tmp_path / "report.json")
    assert payload["status"] == "R5_1_OFFLINE_ACCEPTANCE_PASS"
    assert payload["required_failures"] == []
