from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from freqtrade.hedge.control.models import ControlAction, ControlOutcome, ControlRequest
from freqtrade.hedge.control.store import SqlControlOperationStore
from freqtrade.persistence.hedge_models import ControlOperationRow
from freqtrade.rpc.api_server.hedge_control import create_hedge_control_router

from test_r33_control_plane import (
    ACCOUNT,
    build_service,
    principal,
    req,
)
from freqtrade.hedge.integration.production_main_loop import HedgeExecutionMode
from freqtrade.hedge.control.auth import HedgeRole


def test_sql_store_is_durable_and_detects_payload_conflicts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite'}")
    ControlOperationRow.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service, _ = build_service(sql_store=SqlControlOperationStore(factory))
    stop = req(ControlAction.STOP_NEW_ORDERS, "sql-stop")
    first = service.execute(principal=principal(HedgeRole.OPERATOR), request=stop)

    service2, _ = build_service(sql_store=SqlControlOperationStore(factory))
    replay = service2.execute(principal=principal(HedgeRole.OPERATOR), request=stop)
    assert replay.operation_id == first.operation_id
    assert replay.outcome is ControlOutcome.REPLAYED


def test_rest_router_exposes_status_confirmation_and_operation():
    service, _ = build_service(HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED)

    def admin():
        return principal(HedgeRole.ADMIN)

    app = FastAPI()
    app.include_router(create_hedge_control_router(service, principal_dependency=admin))
    client = TestClient(app)
    assert client.get("/hedge/control/status").status_code == 200
    body = {
        "action": "CLOSE_LONG",
        "account_id": ACCOUNT,
        "idempotency_key": "api-close",
        "reason": "api test",
        "symbol": "ETHUSDT",
    }
    confirmation = client.post("/hedge/control/confirmations", json=body)
    assert confirmation.status_code == 200
    token = confirmation.json()["token"]
    operation = client.post(
        "/hedge/control/operations",
        json={**body, "confirmation_token": token},
    )
    assert operation.status_code == 200
    assert operation.json()["code"] == "LIVE_WRITE_LOCKED"


def test_control_operation_migration_create_and_verify(tmp_path):
    from freqtrade.persistence.hedge_migrations import (
        _step_add_control_operation_leases,
        _step_create_control_operations,
        _step_verify_control_operation_leases,
        _step_verify_control_operations,
        migration_plan_ids,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    created = _step_create_control_operations(engine)
    leased = _step_add_control_operation_leases(engine)
    verified = _step_verify_control_operations(engine)
    lease_verified = _step_verify_control_operation_leases(engine)
    assert created["table"] == "hedge_control_operations"
    assert leased["table"] == "hedge_control_operations"
    assert verified == {"table": "hedge_control_operations", "verified": True}
    assert lease_verified == {"table": "hedge_control_operations", "verified": True}
    plan = migration_plan_ids()
    assert plan[-6:] == (
        "H3-029-durable-control-operations",
        "H3-030-verify-durable-control-operations",
        "H3-031-control-operation-leases",
        "H3-032-verify-control-operation-leases",
        "H3-033-r5-authority-ledger",
        "H3-034-verify-r5-authority-ledger",
    )
