"""Optional Hedge REST/WebSocket route registration.

Kept outside :mod:`webserver` so future upstream upgrades need only preserve a
single stable hook before the UI catch-all router.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException


def register_hedge_api_routes(app: FastAPI, config: dict[str, Any], api_server: object) -> None:
    """Register fail-closed Hedge routes when the feature gate is enabled."""

    if not config.get("hedge_mode_enabled", False):
        return

    from freqtrade.rpc.api_server.api_auth import get_user_from_token, http_basic_or_jwt_token
    from freqtrade.rpc.api_server.hedge_auth import HedgePrincipal, HedgeRole
    from freqtrade.rpc.api_server.hedge_readonly import (
        create_disabled_hedge_write_router,
        create_hedge_readonly_router,
    )
    from freqtrade.rpc.api_server.hedge_runtime import (
        HedgeExecutionRuntimeQuery,
        HedgeRuntimeQuery,
    )
    from freqtrade.rpc.api_server.hedge_ws import (
        HedgeEventHub,
        HedgeWsPrincipal,
        create_hedge_ws_router,
    )

    api_config = config["api_server"]
    raw_role_map = api_config.get("hedge_roles", {})
    role_map = raw_role_map if isinstance(raw_role_map, dict) else {}

    def hedge_principal(username: str = Depends(http_basic_or_jwt_token)) -> HedgePrincipal:
        raw_role = str(role_map.get(username, "VIEWER")).strip().upper()
        try:
            role = HedgeRole[raw_role]
        except KeyError as exc:
            raise HTTPException(status_code=403, detail="invalid configured hedge role") from exc
        return HedgePrincipal(subject=username, role=role)

    raw_ws_accounts = api_config.get("hedge_ws_accounts", {})
    ws_accounts = raw_ws_accounts if isinstance(raw_ws_accounts, dict) else {}

    def _accounts_for(subject: str) -> frozenset[str] | None:
        raw = ws_accounts.get(subject)
        if raw is None:
            return None
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(str(item).strip() for item in raw if str(item).strip())

    def _principal(subject: str) -> HedgeWsPrincipal | None:
        raw_role = str(role_map.get(subject, "VIEWER")).strip().upper()
        try:
            role = HedgeRole[raw_role]
        except KeyError:
            return None
        if role < HedgeRole.VIEWER:
            return None
        accounts = _accounts_for(subject)
        if accounts == frozenset():
            return None
        return HedgeWsPrincipal(
            subject=subject,
            role=role,
            account_ids=accounts,
            allow_sensitive=role >= HedgeRole.ADMIN
            and bool(api_config.get("hedge_ws_allow_sensitive_admin", False)),
        )

    def validate_hedge_ws_token(token: str | None) -> HedgeWsPrincipal | bool:
        if token is None:
            return False
        configured = api_config.get("ws_token")
        if isinstance(configured, str) and secrets.compare_digest(configured, token):
            return _principal("ws-service") or False
        if isinstance(configured, list) and any(
            isinstance(candidate, str) and secrets.compare_digest(candidate, token)
            for candidate in configured
        ):
            return _principal("ws-service") or False
        try:
            username = get_user_from_token(token, api_config["jwt_secret_key"])
        except HTTPException:
            return False
        return _principal(str(username)) or False

    hedge_event_hub = HedgeEventHub()
    app.state.hedge_event_hub = hedge_event_hub
    app.include_router(
        create_hedge_readonly_router(
            HedgeRuntimeQuery(),
            principal_dependency=hedge_principal,
            execution_query=HedgeExecutionRuntimeQuery(),
        ),
        prefix="/api/v1",
    )
    rpc = getattr(api_server, "_rpc", None)
    bot = getattr(rpc, "_freqtrade", None)
    composition = getattr(bot, "hedge_composition", None)
    control_service = getattr(composition, "control_service", None)
    if control_service is None:
        app.include_router(create_disabled_hedge_write_router(), prefix="/api/v1")
    else:
        from freqtrade.rpc.api_server.hedge_control import create_hedge_control_router

        app.include_router(
            create_hedge_control_router(
                control_service,
                principal_dependency=hedge_principal,
            ),
            prefix="/api/v1",
        )

    application = getattr(getattr(rpc, "_freqtrade", None), "hedge_application", None)
    publisher = getattr(getattr(application, "execution", None), "publisher", None)
    set_callback = getattr(publisher, "set_callback", None)
    if callable(set_callback):
        from freqtrade.hedge.execution.event_publisher import HedgeEventHubPublisher

        set_callback(HedgeEventHubPublisher(hedge_event_hub).publish)

    app.include_router(
        create_hedge_ws_router(hedge_event_hub, token_validator=validate_hedge_ws_token),
        prefix="/api/v1",
    )

    hedge_config = config.get("hedge", {})
    dashboard = hedge_config.get("dashboard", {}) if isinstance(hedge_config, dict) else {}
    if isinstance(dashboard, dict) and bool(dashboard.get("enabled", False)):
        from ipaddress import ip_address
        from freqtrade.rpc.api_server.hedge_dashboard import (
            HedgeDashboardQuery, create_hedge_dashboard_router, create_hedge_dashboard_ui_router,
        )
        listen_ip = str(api_config.get("listen_ip_address", "127.0.0.1"))
        normalized_listen_ip = "127.0.0.1" if listen_ip.lower() == "localhost" else listen_ip
        if bool(dashboard.get("local_only", True)):
            try:
                if not ip_address(normalized_listen_ip).is_loopback:
                    raise RuntimeError("R5.5 local dashboard requires a loopback API listen address")
            except ValueError as exc:
                raise RuntimeError("R5.5 dashboard listen address is invalid") from exc
        account_id = str(hedge_config.get("account_id", "hedge-main"))
        symbol = str(config.get("managed_pair") or "")
        query = HedgeDashboardQuery(
            account_id=account_id, symbol=symbol,
            refresh_seconds=int(dashboard.get("refresh_seconds", 5)),
        )
        app.include_router(
            create_hedge_dashboard_router(query=query, principal_dependency=hedge_principal),
            prefix="/api/v1",
        )
        app.include_router(create_hedge_dashboard_ui_router())
