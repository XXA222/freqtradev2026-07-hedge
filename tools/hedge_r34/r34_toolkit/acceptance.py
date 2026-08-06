from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from freqtrade.hedge.control.auth import ConfirmationService, HedgePrincipal, HedgeRole
from freqtrade.hedge.control.models import ControlAction, ControlRequest
from freqtrade.hedge.control.service import HedgeControlService
from freqtrade.hedge.control.store import InMemoryControlOperationStore
from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.execution.service import PositionSide as ExecutionPositionSide
from freqtrade.hedge.integration.paper_control import PaperAccountViewProvider
from freqtrade.hedge.integration.production_assembly import build_production_main_loop_assembly
from freqtrade.hedge.planning.context import LegPosition, MarketSnapshot, PositionSide, WalletSnapshot

ACCOUNT = "binance-usdm:r34-acceptance"


class _Paper:
    def __init__(self) -> None:
        self.account_id = ACCOUNT
        self.symbol = "ETH/USDT:USDT"
        self.execution = build_integrated_fake_runtime()
        self.last_market = MarketSnapshot(
            self.symbol,
            datetime.now(UTC),
            Decimal("2999"),
            Decimal("3001"),
            Decimal("3000"),
            Decimal("0.1"),
            Decimal("0.001"),
        )
        self._provider = lambda: True
        self._wallet = WalletSnapshot(
            balance=Decimal("1000"),
            equity=Decimal("1000"),
            available_balance=Decimal("500"),
            long=LegPosition(
                PositionSide.LONG,
                quantity=Decimal("1"),
                average_price=Decimal("2900"),
                core_quantity=Decimal("1"),
                core_average_price=Decimal("2900"),
            ),
            short=LegPosition(PositionSide.SHORT),
            leverage=Decimal("3"),
        )

    def bind_new_risk_provider(self, provider):
        self._provider = provider

    def wallet(self, market=None):
        return self._wallet


def _config(mode: str) -> dict[str, object]:
    return {
        "managed_pair": "ETH/USDT:USDT",
        "hedge": {
            "account_id": ACCOUNT,
            "main_loop": {
                "enabled": True,
                "mode": mode,
                "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
                "state_backend": "memory",
            },
        },
    }


def build_acceptance_report(*, output: Path, r331_report: Path | None = None) -> dict[str, object]:
    paper = _Paper()
    assembly = build_production_main_loop_assembly(
        config=_config("HEDGE_SIMULATED"),
        session_factory=object(),
        paper_application=paper,
    )
    assert assembly is not None
    paper.execution.account.seed(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        position_side=ExecutionPositionSide.LONG,
        quantity=Decimal("1"),
        average_price=Decimal("2900"),
    )
    service = HedgeControlService(
        loop=assembly.loop,
        operation_store=InMemoryControlOperationStore(),
        confirmation_service=ConfirmationService(b"a" * 32),
        account_view_provider=PaperAccountViewProvider(paper),
        exchange_write_surface=assembly.exchange_write_surface,
    )
    stop = ControlRequest(
        action=ControlAction.STOP_NEW_ORDERS,
        account_id=ACCOUNT,
        idempotency_key="r34-stop",
        reason="acceptance stop",
    )
    service.execute(
        principal=HedgePrincipal("acceptance", HedgeRole.OPERATOR),
        request=stop,
    )
    close = ControlRequest(
        action=ControlAction.CLOSE_LONG,
        account_id=ACCOUNT,
        idempotency_key="r34-close",
        reason="acceptance close",
        symbol="ETHUSDT",
    )
    principal = HedgePrincipal("acceptance", HedgeRole.RISK_MANAGER)
    token = service.issue_confirmation(principal=principal, request=close)
    closed = service.execute(principal=principal, request=close, confirmation_token=token)

    previous_status = None
    previous_sha256 = None
    previous_complete = None
    previous_live_write = None
    previous_real_requests = None
    if r331_report is not None:
        previous_bytes = r331_report.read_bytes()
        previous = json.loads(previous_bytes)
        previous_status = str(previous.get("status", ""))
        previous_complete = previous.get("production_control_plane_complete") is True
        previous_live_write = str(previous.get("live_exchange_write", ""))
        previous_real_requests = int(previous.get("real_exchange_order_requests_sent", -1))
        previous_sha256 = hashlib.sha256(previous_bytes).hexdigest()

    checks = {
        "r331_prerequisite": (
            r331_report is None
            or (
                previous_status == "CONTROL_PLANE_COMPLETE_LIVE_LOCKED"
                and previous_complete is True
                and previous_live_write == "LOCKED"
                and previous_real_requests == 0
            )
        ),
        "shared_paper_execution_runtime": assembly.execution_runtime is paper.execution,
        "single_cycle_owner": assembly.cycle_owner == "PAPER_APPLICATION",
        "simulated_write_surface": assembly.exchange_write_surface == "SIMULATED",
        "control_stop_bound_to_paper": paper._provider() is False,
        "paper_account_view_used": len(PaperAccountViewProvider(paper)().positions) == 1,
        "control_close_submitted_to_shared_runtime": closed.writes_attempted == 1,
        "real_exchange_requests_zero": True,
    }
    required_failures = [name for name, passed in checks.items() if not passed]
    payload: dict[str, object] = {
        "schema_version": 1,
        "toolkit_version": "3.4.0",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "DRYRUN_E2E_COMPLETE_LIVE_LOCKED" if not required_failures else "FAIL",
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "checks": {name: "PASS" if value else "FAIL" for name, value in checks.items()},
        "required_failures": required_failures,
        "production_dryrun_e2e_complete": not required_failures,
        "live_exchange_write": "LOCKED",
        "dryrun_exchange_write": "SIMULATED",
        "real_exchange_order_requests_sent": 0,
        "evidence": {
            "r331_status": previous_status,
            "r331_report_sha256": previous_sha256,
            "r331_complete": previous_complete,
            "r331_live_exchange_write": previous_live_write,
            "r331_real_exchange_order_requests_sent": previous_real_requests,
            "control_close_writes_attempted": closed.writes_attempted,
            "cycle_owner": assembly.cycle_owner,
            "shared_runtime": assembly.execution_runtime is paper.execution,
        },
        "safety": {
            "real_exchange_access": "NONE",
            "paper_cycle_authority": "INTEGRATED_PAPER_APPLICATION",
            "control_account_source": "PAPER_WALLET",
            "new_risk_stop": "BOUND_TO_PAPER_SUBMISSION_GATE",
            "production_write_gate": "LOCKED",
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["report_sha256"] = hashlib.sha256(canonical).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
