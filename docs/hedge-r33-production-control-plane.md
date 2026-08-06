# R3.3 Production Control Plane

## Scope

R3.3 binds authenticated control operations to the R3.2 production-equivalent main
program. The supported market scope remains BTCUSDT and ETHUSDT USD-M perpetuals.

## Roles

- VIEWER: read control status.
- OPERATOR: stop new risk and activate the kill switch.
- RISK_MANAGER: resume new risk, cancel managed orders, and request position closes.
- ADMIN: release the kill switch.

Dangerous operations use one-time tokens scoped to actor, action, account, symbol,
request payload, and idempotency key.

## Operations

- STOP_NEW_ORDERS
- RESUME_NEW_ORDERS
- KILL_SWITCH_ACTIVATE
- KILL_SWITCH_RELEASE
- CANCEL_MANAGED_ORDERS
- CLOSE_LONG
- CLOSE_SHORT
- CLOSE_BOTH

## Safety

Production-locked mode creates a durable operation record and returns a deterministic
plan, but performs zero exchange writes. Order cancellation is restricted to orders
owned by the durable execution store. External or prefix-only orders are never
claimed or canceled.

## REST routes

When `hedge.control_plane.enabled` is true and the R3.2 composition is available:

- `GET /api/v1/hedge/control/status`
- `POST /api/v1/hedge/control/confirmations`
- `POST /api/v1/hedge/control/operations`

Roles are resolved from `api_server.hedge_roles`.

## Persistence

R3.3 registers migrations H3-029 and H3-030 for `hedge_control_operations`.
The record stores the canonical request hash, actor, role, lifecycle state, outcome,
and complete result JSON. The unique `(account_id, idempotency_key)` constraint is
the durable authority for replay and payload-conflict detection.
