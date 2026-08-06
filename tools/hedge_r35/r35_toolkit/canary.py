from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from freqtrade.hedge.execution.service import PositionSide
from freqtrade.hedge.execution.testnet import (
    build_guarded_testnet_runtime,
    build_testnet_readonly_runtime,
    evidence_from_ready_testnet_readonly,
    load_binance_testnet_credentials,
    make_testnet_limit_intent,
    run_submit_cancel_canary,
)


async def run_real_testnet_canary(
    *,
    credential_file: Path,
    report_file: Path,
    mode: str,
    symbol: str,
    position_side: str,
    quantity: Decimal,
    limit_price: Decimal,
    max_notional: Decimal,
    proxy_url: str | None,
    ready_timeout_seconds: float,
    confirmed_testnet: bool,
    confirmed_submit_cancel: bool,
) -> dict[str, object]:
    if not confirmed_testnet:
        raise PermissionError("--confirmed-testnet is required")
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"validation-only", "submit-cancel"}:
        raise ValueError("mode must be validation-only or submit-cancel")
    if normalized_mode == "submit-cancel" and not confirmed_submit_cancel:
        raise PermissionError("--confirmed-submit-cancel is required")
    token = os.environ.get("FREQTRADE_HEDGE_TESTNET_ARM_TOKEN", "").strip()
    if len(token) < 16:
        raise RuntimeError("FREQTRADE_HEDGE_TESTNET_ARM_TOKEN must contain at least 16 characters")
    expected_hash = os.environ.get(
        "FREQTRADE_HEDGE_TESTNET_ARM_TOKEN_SHA256", ""
    ).strip().lower()
    if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
        raise RuntimeError(
            "FREQTRADE_HEDGE_TESTNET_ARM_TOKEN_SHA256 must be independently configured"
        )
    credentials = load_binance_testnet_credentials(credential_file)
    readonly = build_testnet_readonly_runtime(
        credentials=credentials,
        proxy_url=proxy_url,
    )
    started = datetime.now(UTC)
    payload: dict[str, object]
    async with readonly:
        await readonly.wait_until_ready(timeout_seconds=ready_timeout_seconds)
        evidence = evidence_from_ready_testnet_readonly(
            readonly=readonly,
            credentials=credentials,
            expected_arm_token_sha256=expected_hash,
            max_order_notional=max_notional,
            futures_trading_permission=True,
            allow_market_orders=False,
        )
        guarded = build_guarded_testnet_runtime(
            credentials=credentials,
            evidence=evidence,
            readonly=readonly,
            proxy_url=proxy_url,
        )
        guarded.arm(token=token, actor="r35-testnet-canary", confirmed=True, ttl_seconds=300)
        intent = make_testnet_limit_intent(
            credentials=credentials,
            symbol=symbol,
            position_side=PositionSide(position_side.upper()),
            quantity=quantity,
            limit_price=limit_price,
            idempotency_key=f"r35-{normalized_mode}-{int(started.timestamp())}",
        )
        if normalized_mode == "validation-only":
            validation = guarded.validate_order(intent)
            result: dict[str, object] = {
                "accepted": validation.accepted,
                "client_order_id": validation.client_order_id,
                "symbol": validation.symbol,
                "real_order_created": False,
            }
        else:
            canary = run_submit_cancel_canary(guarded=guarded, intent=intent)
            result = {
                "clean": canary.clean,
                "client_order_id": canary.client_order_id,
                "submitted_status": canary.submitted_status,
                "cancel_status": canary.cancel_status,
                "unexpected_fill": canary.unexpected_fill,
                "exchange_order_id": canary.exchange_order_id,
                "write_requests": canary.write_requests,
            }
        telemetry = guarded.runtime.exchange.telemetry()
        payload = {
            "schema_version": 1,
            "toolkit_version": "3.5.0",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "mode": normalized_mode,
            "environment": "BINANCE_USDM_TESTNET",
            "account_id": credentials.account_id,
            "symbol": symbol.upper(),
            "position_side": position_side.upper(),
            "quantity": str(quantity),
            "limit_price": str(limit_price),
            "max_notional": str(max_notional),
            "result": result,
            "telemetry": {
                "logical_requests": telemetry.logical_requests,
                "write_requests": telemetry.write_requests,
                "read_requests": telemetry.read_requests,
                "ambiguous_write_errors": telemetry.ambiguous_write_errors,
                "definitive_write_errors": telemetry.definitive_write_errors,
            },
            "mainnet_access": "NONE",
            "mainnet_live_exchange_write": "LOCKED",
            "started_at_utc": started.isoformat(),
        }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["report_sha256"] = hashlib.sha256(canonical).hexdigest()
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run(**kwargs: object) -> dict[str, object]:
    return asyncio.run(run_real_testnet_canary(**kwargs))
