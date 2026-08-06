from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from freqtrade.hedge.control.models import (
    ControlAction,
    ControlOutcome,
    ControlRequest,
)
from freqtrade.hedge.control.service import (
    ControlConfirmationError,
    ControlPermissionError,
    HedgeControlService,
)
from freqtrade.hedge.control.store import (
    ControlOperationConflict,
    InMemoryControlOperationStore,
    SqlControlOperationStore,
)
from freqtrade.hedge.execution.integrated_fake import build_integrated_fake_runtime
from freqtrade.hedge.execution.ownership import ExecutionOrderOwnershipRegistry
from freqtrade.hedge.execution.service import (
    IntentAction,
    OrderIntent,
    OrderType,
    PositionSide,
)
from freqtrade.hedge.integration.production_main_loop import (
    ExecutionEngineKind,
    HedgeExecutionMode,
    ProductionEquivalentHedgeMainLoop,
)
from freqtrade.hedge.control.auth import (
    ConfirmationService,
    HedgePrincipal,
    HedgeRole,
)

ACCOUNT = "binance-usdm:test"


def account_view(*, long_qty="1", short_qty="2"):
    rows = []
    if Decimal(long_qty) > 0:
        rows.append(
            SimpleNamespace(
                symbol="ETHUSDT",
                position_side="LONG",
                quantity=Decimal(long_qty),
                mark_price=Decimal("3000"),
            )
        )
    if Decimal(short_qty) > 0:
        rows.append(
            SimpleNamespace(
                symbol="ETHUSDT",
                position_side="SHORT",
                quantity=Decimal(short_qty),
                mark_price=Decimal("3000"),
            )
        )
    return SimpleNamespace(account_id=ACCOUNT, positions=tuple(rows))


def build_service(mode=HedgeExecutionMode.HEDGE_SIMULATED, *, sql_store=None):
    runtime = build_integrated_fake_runtime()
    kind = (
        ExecutionEngineKind.SIMULATED
        if mode is HedgeExecutionMode.HEDGE_SIMULATED
        else ExecutionEngineKind.PRODUCTION
    )
    loop = ProductionEquivalentHedgeMainLoop(
        account_id=ACCOUNT,
        engine=runtime.engine,
        ownership=ExecutionOrderOwnershipRegistry(runtime.store),
        kill_switch=runtime.kill_switch,
        mode=mode,
        engine_kind=kind,
    )
    service = HedgeControlService(
        loop=loop,
        operation_store=sql_store or InMemoryControlOperationStore(),
        confirmation_service=ConfirmationService(b"c" * 32),
        account_view_provider=account_view,
        exchange_write_surface=("NONE" if mode is HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED else "SIMULATED"),
    )
    return service, runtime


def req(action, key, **kwargs):
    return ControlRequest(
        action=action,
        account_id=ACCOUNT,
        idempotency_key=key,
        reason="pytest control action",
        **kwargs,
    )


def principal(role=HedgeRole.ADMIN):
    return HedgePrincipal("pytest", role)


def confirm(service, request, role=HedgeRole.ADMIN):
    user = principal(role)
    return user, service.issue_confirmation(principal=user, request=request)


def test_rbac_and_confirmation_are_fail_closed():
    service, _ = build_service()
    close = req(ControlAction.CLOSE_LONG, "close-1", symbol="ETHUSDT")
    with pytest.raises(ControlPermissionError):
        service.issue_confirmation(principal=principal(HedgeRole.VIEWER), request=close)
    with pytest.raises(ControlConfirmationError):
        service.execute(principal=principal(HedgeRole.RISK_MANAGER), request=close)


def test_stop_and_resume_new_risk_require_different_safety_levels():
    service, _ = build_service()
    stop = req(ControlAction.STOP_NEW_ORDERS, "stop-1")
    result = service.execute(principal=principal(HedgeRole.OPERATOR), request=stop)
    assert result.outcome is ControlOutcome.SUCCEEDED
    assert service.loop.new_risk_enabled is False

    resume = req(ControlAction.RESUME_NEW_ORDERS, "resume-1")
    risk_manager, token = confirm(service, resume, HedgeRole.RISK_MANAGER)
    resumed = service.execute(
        principal=risk_manager,
        request=resume,
        confirmation_token=token,
    )
    assert resumed.code == "NEW_RISK_RESUMED"
    assert service.loop.new_risk_enabled is True
    replay = service.execute(
        principal=risk_manager,
        request=resume,
        confirmation_token=token,
    )
    assert replay.outcome is ControlOutcome.REPLAYED


def test_idempotent_replay_does_not_repeat_side_effect():
    service, _ = build_service()
    stop = req(ControlAction.STOP_NEW_ORDERS, "stop-replay")
    first = service.execute(principal=principal(HedgeRole.OPERATOR), request=stop)
    replay = service.execute(principal=principal(HedgeRole.OPERATOR), request=stop)
    assert replay.outcome is ControlOutcome.REPLAYED
    assert replay.operation_id == first.operation_id
    assert replay.writes_attempted == 0


def test_same_idempotency_key_with_different_payload_is_conflict():
    service, _ = build_service()
    first = req(ControlAction.STOP_NEW_ORDERS, "conflict")
    service.execute(principal=principal(HedgeRole.OPERATOR), request=first)
    changed = ControlRequest(
        action=ControlAction.STOP_NEW_ORDERS,
        account_id=ACCOUNT,
        idempotency_key="conflict",
        reason="different reason",
    )
    with pytest.raises(ControlOperationConflict):
        service.execute(principal=principal(HedgeRole.OPERATOR), request=changed)


def test_production_locked_close_returns_plan_with_zero_writes():
    service, _ = build_service(HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED)
    close = req(ControlAction.CLOSE_BOTH, "locked-close", symbol="ETHUSDT")
    user, token = confirm(service, close, HedgeRole.RISK_MANAGER)
    result = service.execute(principal=user, request=close, confirmation_token=token)
    assert result.outcome is ControlOutcome.BLOCKED
    assert result.code == "LIVE_WRITE_LOCKED"
    assert result.writes_attempted == 0
    assert {item.position_side for item in result.planned} == {"LONG", "SHORT"}


def test_simulated_close_long_submits_reduce_only_order():
    service, runtime = build_service()
    runtime.account.seed(
        account_id=ACCOUNT,
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_price=Decimal("2900"),
    )
    close = req(ControlAction.CLOSE_LONG, "sim-close-long", symbol="ETHUSDT")
    user, token = confirm(service, close, HedgeRole.RISK_MANAGER)
    result = service.execute(principal=user, request=close, confirmation_token=token)
    assert result.outcome is ControlOutcome.SUCCEEDED
    assert result.writes_attempted == 1
    order = runtime.core.get_order(result.executed_references[0])
    assert order.intent.reduce_only is True
    assert order.intent.position_side is PositionSide.LONG
    assert order.intent.action is IntentAction.CLOSE


def test_cancel_managed_orders_never_claims_external_order():
    service, runtime = build_service()
    managed = runtime.engine.submit(
        OrderIntent(
            account_id=ACCOUNT,
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            action=IntentAction.OPEN,
            quantity=Decimal("0.01"),
            idempotency_key="managed-order",
            order_type=OrderType.LIMIT,
            limit_price=Decimal("3000"),
        )
    )
    cancel = req(ControlAction.CANCEL_MANAGED_ORDERS, "cancel-managed", symbol="ETHUSDT")
    user, token = confirm(service, cancel, HedgeRole.RISK_MANAGER)
    result = service.execute(principal=user, request=cancel, confirmation_token=token)
    assert result.executed_references == (managed.order.client_order_id,)
    assert runtime.core.get_order(managed.order.client_order_id).lifecycle.terminal


def test_kill_switch_release_requires_admin_and_confirmation():
    service, _ = build_service()
    halt = req(ControlAction.KILL_SWITCH_ACTIVATE, "halt")
    service.execute(principal=principal(HedgeRole.OPERATOR), request=halt)
    release = req(ControlAction.KILL_SWITCH_RELEASE, "release")
    with pytest.raises(ControlPermissionError):
        service.issue_confirmation(principal=principal(HedgeRole.RISK_MANAGER), request=release)
    admin, token = confirm(service, release, HedgeRole.ADMIN)
    result = service.execute(principal=admin, request=release, confirmation_token=token)
    assert result.code == "KILL_SWITCH_RUNNING"


def test_status_reports_fixed_symbols_and_live_lock():
    service, _ = build_service(HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED)
    status = service.status()
    assert status.allowed_symbols == ("BTCUSDT", "ETHUSDT")
    assert status.live_exchange_write == "LOCKED"


def test_restore_state_reapplies_durable_halt_without_replaying_cancellations():
    store = InMemoryControlOperationStore()
    service, _ = build_service(sql_store=store)
    stop = req(ControlAction.STOP_NEW_ORDERS, "durable-stop")
    service.execute(principal=principal(HedgeRole.OPERATOR), request=stop)
    halt = req(ControlAction.KILL_SWITCH_ACTIVATE, "durable-halt")
    service.execute(principal=principal(HedgeRole.OPERATOR), request=halt)

    restored, _ = build_service(sql_store=store)
    assert restored.loop.new_risk_enabled is True
    restored.restore_state()
    assert restored.loop.new_risk_enabled is False
    assert restored.loop.kill_switch.snapshot().mode.value == "HALTED"


def test_control_plane_config_is_typed_and_rejects_unknown_keys():
    from freqtrade.exceptions import OperationalException
    from freqtrade.hedge.control.config import hedge_control_plane_config_from_mapping

    parsed = hedge_control_plane_config_from_mapping(
        {
            "hedge": {
                "control_plane": {
                    "enabled": True,
                    "confirmation_ttl_seconds": 90,
                    "max_pending_confirmations": 500,
                }
            }
        }
    )
    assert parsed.enabled is True
    assert parsed.confirmation_ttl_seconds == 90
    assert parsed.max_pending_confirmations == 500

    with pytest.raises(OperationalException):
        hedge_control_plane_config_from_mapping(
            {"hedge": {"control_plane": {"unexpected": True}}}
        )


def test_confirmation_token_is_bound_to_payload_and_one_time():
    service, _ = build_service()
    close = req(ControlAction.CLOSE_LONG, "bound-token", symbol="ETHUSDT")
    user = principal(HedgeRole.RISK_MANAGER)
    token = service.issue_confirmation(principal=user, request=close)
    changed = ControlRequest(
        action=ControlAction.CLOSE_LONG,
        account_id=ACCOUNT,
        idempotency_key="bound-token",
        reason="changed payload",
        symbol="ETHUSDT",
    )
    with pytest.raises(ControlConfirmationError):
        service.execute(principal=user, request=changed, confirmation_token=token)

    result = service.execute(principal=user, request=close, confirmation_token=token)
    assert result.outcome is ControlOutcome.SUCCEEDED

    changed_token = service.issue_confirmation(principal=user, request=changed)
    with pytest.raises(ControlOperationConflict):
        service.execute(
            principal=user,
            request=changed,
            confirmation_token=changed_token,
        )

    replay = service.execute(principal=user, request=close, confirmation_token=token)
    assert replay.outcome is ControlOutcome.REPLAYED


def test_locked_kill_switch_performs_zero_execution_writes():
    service, runtime = build_service(HedgeExecutionMode.HEDGE_PRODUCTION_LOCKED)
    managed = runtime.engine.submit(
        OrderIntent(
            account_id=ACCOUNT,
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            action=IntentAction.OPEN,
            quantity=Decimal("0.01"),
            idempotency_key="locked-kill-managed",
            order_type=OrderType.LIMIT,
            limit_price=Decimal("3000"),
        )
    )
    halt = req(ControlAction.KILL_SWITCH_ACTIVATE, "locked-halt")
    result = service.execute(principal=principal(HedgeRole.OPERATOR), request=halt)
    assert result.code == "KILL_SWITCH_HALTED"
    assert result.writes_attempted == 0
    assert result.executed_references == ()
    assert [item.reference for item in result.planned] == [managed.order.client_order_id]
    assert runtime.core.get_order(managed.order.client_order_id).lifecycle.terminal is False
    assert service.loop.new_risk_enabled is False
