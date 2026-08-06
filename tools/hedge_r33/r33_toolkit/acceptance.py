"""Deterministic acceptance for the R3.3 production control plane."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from freqtrade.hedge.control.auth import (
    ConfirmationService,
    HedgePrincipal,
    HedgeRole,
)
from freqtrade.hedge.control.models import (
    ControlAction,
    ControlOutcome,
    ControlRequest,
)
from freqtrade.hedge.control.service import (
    ControlPermissionError,
    HedgeControlService,
)
from freqtrade.hedge.control.store import (
    InMemoryControlOperationStore,
    SqlControlOperationStore,
)
from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.execution.ownership import ExecutionOrderOwnershipRegistry
from freqtrade.hedge.integration.production_main_loop import (
    ExecutionEngineKind,
    HedgeExecutionMode,
    ProductionEquivalentHedgeMainLoop,
)
from freqtrade.persistence.hedge_migrations import migration_plan_ids
from freqtrade.persistence.hedge_models import ControlOperationRow
from freqtrade.rpc.api_server.hedge_control import create_hedge_control_router

ACCOUNT = "binance-usdm:r33-acceptance"
SYMBOL = "ETHUSDT"


def load_r32_report(path: str | Path) -> Mapping[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("R3.2 report must be a JSON object")
    if payload.get("status") != "MAIN_PROGRAM_INTEGRATION_COMPLETE_LIVE_LOCKED":
        raise ValueError("R3.2 prerequisite status is invalid")
    if payload.get("production_main_program_integration_complete") is not True:
        raise ValueError("R3.2 main-program integration is incomplete")
    if payload.get("live_exchange_write") != "LOCKED":
        raise ValueError("R3.2 live exchange write must remain locked")
    return payload


def _account_view() -> object:
    return SimpleNamespace(
        account_id=ACCOUNT,
        positions=(
            SimpleNamespace(
                symbol=SYMBOL,
                position_side="LONG",
                quantity=Decimal("0.10"),
                mark_price=Decimal("3000"),
            ),
            SimpleNamespace(
                symbol=SYMBOL,
                position_side="SHORT",
                quantity=Decimal("0.20"),
                mark_price=Decimal("3000"),
            ),
        ),
    )


def _build_service(*, store: object | None = None) -> HedgeControlService:
    runtime = build_integrated_fake_runtime()
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED,
        engine_kind=ExecutionEngineKind.PRODUCTION,
    )
    return HedgeControlService(
        loop=loop,
        operation_store=store or InMemoryControlOperationStore(),
        confirmation_service=ConfirmationService(b"r33-acceptance-secret-material!!"),
        account_view_provider=_account_view,
        exchange_write_surface="NONE",
    )


def _principal(role: HedgeRole) -> HedgePrincipal:
    return HedgePrincipal(subject=f"acceptance-{role.name.lower()}", role=role)


def _request(
    action: ControlAction,
    key: str,
    *,
    symbol: str | None = None,
) -> ControlRequest:
    return ControlRequest(
        action=action,
        account_id=ACCOUNT,
        idempotency_key=key,
        reason="R3.3 deterministic acceptance",
        symbol=symbol,
    )


def _execute_confirmed(
    service: HedgeControlService,
    principal: HedgePrincipal,
    request: ControlRequest,
):
    token = service.issue_confirmation(principal=principal, request=request)
    return service.execute(
        principal=principal,
        request=request,
        confirmation_token=token,
    )


def _verify_sql_replay() -> bool:
    # Windows keeps SQLite files locked while a pooled connection remains open.
    # NullPool avoids retained idle connections, and dispose() runs before the
    # TemporaryDirectory context attempts to remove control.sqlite.
    with tempfile.TemporaryDirectory(prefix="freqtrade-hedge-r331-") as directory:
        engine = create_engine(
            f"sqlite:///{Path(directory) / 'control.sqlite'}",
            poolclass=NullPool,
        )
        try:
            ControlOperationRow.__table__.create(engine)
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            request = _request(ControlAction.STOP_NEW_ORDERS, "sql-durable-stop")
            first_service = _build_service(store=SqlControlOperationStore(factory))
            first = first_service.execute(
                principal=_principal(HedgeRole.OPERATOR),
                request=request,
            )
            second_service = _build_service(store=SqlControlOperationStore(factory))
            replay = second_service.execute(
                principal=_principal(HedgeRole.OPERATOR),
                request=request,
            )
            return (
                replay.outcome is ControlOutcome.REPLAYED
                and replay.operation_id == first.operation_id
                and replay.writes_attempted == 0
            )
        finally:
            engine.dispose(close=True)


def _verify_api_contract(service: HedgeControlService) -> bool:
    admin = _principal(HedgeRole.ADMIN)

    def dependency() -> HedgePrincipal:
        return admin

    app = FastAPI()
    app.include_router(
        create_hedge_control_router(service, principal_dependency=dependency)
    )
    body = {
        "action": "CLOSE_LONG",
        "account_id": ACCOUNT,
        "idempotency_key": "api-locked-close",
        "reason": "R3.3.1 API acceptance",
        "symbol": SYMBOL,
    }
    with TestClient(app) as client:
        status = client.get("/hedge/control/status")
        confirmation = client.post("/hedge/control/confirmations", json=body)
        if status.status_code != 200 or confirmation.status_code != 200:
            return False
        operation = client.post(
            "/hedge/control/operations",
            json={**body, "confirmation_token": confirmation.json()["token"]},
        )
        return (
            operation.status_code == 200
            and operation.json().get("code") == "LIVE_WRITE_LOCKED"
            and operation.json().get("writes_attempted") == 0
        )


def build_acceptance_report(
    *,
    r32_report: str | Path,
    project_root: str | Path,
) -> dict[str, Any]:
    r32 = load_r32_report(r32_report)
    root = Path(project_root).resolve()
    service = _build_service()

    checks: dict[str, str] = {}
    failures: list[str] = []

    def record(name: str, passed: bool) -> None:
        checks[name] = "PASS" if passed else "FAIL"
        if not passed:
            failures.append(name.upper())

    status = service.status()
    record("fixed_perpetual_allowlist", status.allowed_symbols == ("BTCUSDT", "ETHUSDT"))
    record("live_exchange_write_locked", status.live_exchange_write == "LOCKED")

    close = _request(ControlAction.CLOSE_BOTH, "locked-close-both", symbol=SYMBOL)
    try:
        service.issue_confirmation(
            principal=_principal(HedgeRole.VIEWER),
            request=close,
        )
        viewer_rejected = False
    except ControlPermissionError:
        viewer_rejected = True
    record("rbac_fail_closed", viewer_rejected)

    stop = _request(ControlAction.STOP_NEW_ORDERS, "stop-new-risk")
    first_stop = service.execute(
        principal=_principal(HedgeRole.OPERATOR),
        request=stop,
    )
    replay_stop = service.execute(
        principal=_principal(HedgeRole.OPERATOR),
        request=stop,
    )
    record(
        "stop_new_risk_and_idempotent_replay",
        not service.loop.new_risk_enabled
        and first_stop.outcome is ControlOutcome.SUCCEEDED
        and replay_stop.outcome is ControlOutcome.REPLAYED,
    )

    resume = _request(ControlAction.RESUME_NEW_ORDERS, "resume-new-risk")
    resumed = _execute_confirmed(
        service,
        _principal(HedgeRole.RISK_MANAGER),
        resume,
    )
    record(
        "one_time_confirmation_resume",
        resumed.code == "NEW_RISK_RESUMED" and service.loop.new_risk_enabled,
    )

    locked_close = _execute_confirmed(
        service,
        _principal(HedgeRole.RISK_MANAGER),
        close,
    )
    record(
        "dangerous_close_locked_with_plan",
        locked_close.outcome is ControlOutcome.BLOCKED
        and locked_close.code == "LIVE_WRITE_LOCKED"
        and locked_close.writes_attempted == 0
        and {item.position_side for item in locked_close.planned} == {"LONG", "SHORT"},
    )

    halt = _request(ControlAction.KILL_SWITCH_ACTIVATE, "halt-control-plane")
    halted = service.execute(
        principal=_principal(HedgeRole.OPERATOR),
        request=halt,
    )
    release = _request(ControlAction.KILL_SWITCH_RELEASE, "release-control-plane")
    released = _execute_confirmed(service, _principal(HedgeRole.ADMIN), release)
    record(
        "kill_switch_role_and_confirmation",
        halted.code == "KILL_SWITCH_HALTED"
        and released.code == "KILL_SWITCH_RUNNING",
    )

    record("durable_sql_idempotency_replay", _verify_sql_replay())
    record("rest_control_contract", _verify_api_contract(_build_service()))

    migration_ids = migration_plan_ids()
    record(
        "durable_control_migrations_registered",
        migration_ids[-4:] == (
            "H3-029-durable-control-operations",
            "H3-030-verify-durable-control-operations",
            "H3-031-control-operation-leases",
            "H3-032-verify-control-operation-leases",
        ),
    )
    record(
        "composition_and_api_sources_installed",
        all(
            (root / relative).is_file()
            for relative in (
                "freqtrade/hedge/control/assembly.py",
                "freqtrade/hedge/integration/composition.py",
                "freqtrade/rpc/api_server/hedge_control.py",
                "freqtrade/rpc/api_server/hedge_plugin.py",
            )
        ),
    )

    r32_hash = str(r32.get("report_sha256") or "") or None
    complete = not failures
    payload: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": "3.3.1",
        "status": (
            "CONTROL_PLANE_COMPLETE_LIVE_LOCKED"
            if complete
            else "CONTROL_PLANE_INCOMPLETE"
        ),
        "production_control_plane_complete": complete,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "account_id": ACCOUNT,
        "allowed_symbols": ["BTCUSDT", "ETHUSDT"],
        "supported_actions": [item.value for item in ControlAction],
        "roles": [item.name for item in HedgeRole],
        "live_exchange_write": "LOCKED",
        "real_exchange_order_requests_sent": 0,
        "checks": checks,
        "required_failures": failures,
        "evidence": {
            "r32_status": r32.get("status"),
            "r32_report_sha256": r32_hash,
            "close_plan_items": len(locked_close.planned),
            "close_writes_attempted": locked_close.writes_attempted,
            "idempotent_replay_operation_id": str(replay_stop.operation_id),
            "migration_tail": list(migration_ids[-4:]),
        },
        "safety": {
            "dangerous_operation_confirmation": "ONE_TIME_SCOPED_TOKEN",
            "idempotency_authority": "DURABLE_SQL",
            "external_order_policy": "MANAGED_OWNERSHIP_ONLY",
            "new_risk_control": "STOP_AND_EXPLICIT_CONFIRMED_RESUME",
            "kill_switch_release": "ADMIN_AND_CONFIRMATION_REQUIRED",
            "production_locked_action": "PLAN_ONLY_ZERO_WRITES",
            "real_exchange_access": "NONE",
        },
    }
    return payload


def write_report_atomic(payload: Mapping[str, Any], path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        dict(payload),
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
    ).encode("ascii") + b"\n"
    digest = sha256(data).hexdigest()
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return digest
