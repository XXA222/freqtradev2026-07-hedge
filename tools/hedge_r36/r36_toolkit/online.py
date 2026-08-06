from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from freqtrade.hedge.execution.service import PositionSide
from freqtrade.hedge.execution.testnet import load_binance_testnet_credentials
from freqtrade.hedge.execution.testnet_e2e import (
    TestnetE2EConfig,
    TestnetE2EOrchestrator,
    TestnetRunJournal,
    TestnetScenarioMode,
)

ARM_TOKEN_ENV = "FREQTRADE_HEDGE_TESTNET_ARM_TOKEN"
ARM_TOKEN_HASH_ENV = "FREQTRADE_HEDGE_TESTNET_ARM_TOKEN_SHA256"


def run_online(
    *,
    credential_file: Path,
    journal_file: Path,
    report_file: Path,
    mode: str,
    symbol: str,
    position_side: str,
    target_notional,
    max_order_notional,
    passive_offset_bps: int,
    proxy_url: str | None,
    ready_timeout_seconds: float,
    arm_ttl_seconds: int,
    run_id: str | None,
    actor: str,
    confirmed_testnet: bool,
    confirmed_submit_cancel: bool,
) -> dict[str, object]:
    if not confirmed_testnet:
        raise PermissionError("--confirmed-testnet is required")
    selected_mode = TestnetScenarioMode(str(mode).strip().upper().replace("-", "_"))
    if selected_mode is TestnetScenarioMode.SUBMIT_CANCEL and not confirmed_submit_cancel:
        raise PermissionError("--confirmed-submit-cancel is required for submit-cancel")
    token = os.environ.get(ARM_TOKEN_ENV, "").strip()
    if len(token) < 16:
        raise PermissionError(f"{ARM_TOKEN_ENV} must contain at least 16 characters")
    expected_hash = os.environ.get(ARM_TOKEN_HASH_ENV, "").strip().lower()
    if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
        raise PermissionError(
            f"{ARM_TOKEN_HASH_ENV} must be independently configured as a SHA256 digest"
        )
    credentials = load_binance_testnet_credentials(credential_file)
    config = TestnetE2EConfig(
        mode=selected_mode,
        symbol=symbol,
        position_side=PositionSide(str(position_side).strip().upper()),
        target_notional=target_notional,
        max_order_notional=max_order_notional,
        passive_offset_bps=passive_offset_bps,
        ready_timeout_seconds=ready_timeout_seconds,
        arm_ttl_seconds=arm_ttl_seconds,
    )
    journal = TestnetRunJournal(journal_file)
    orchestrator = TestnetE2EOrchestrator(journal=journal)
    try:
        report = asyncio.run(
            orchestrator.run(
                credentials=credentials,
                config=config,
                arm_token=token,
                expected_arm_token_sha256=expected_hash,
                actor=actor,
                proxy_url=proxy_url,
                run_id=run_id,
            )
        )
        payload = report.to_dict()
    except Exception as exc:
        payload = {
            "schema_version": 1,
            "toolkit_version": "3.7.0",
            "status": "FAIL",
            "error": f"{type(exc).__name__}:{str(exc)[:2000]}",
            "run_id": run_id,
            "mode": selected_mode.value,
            "symbol": config.symbol,
            "position_side": config.position_side.value,
            "journal": str(journal_file),
            "mainnet_live_exchange_write": "LOCKED",
            "real_mainnet_order_requests_sent": 0,
        }
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
