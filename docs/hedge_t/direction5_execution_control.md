# Hedge Direction 5 — Execution and Control Plane v1.4

## Recommended entrypoint

`HedgeExecutionEngine` wraps the deterministic `ExecutionService` and enforces, in order:

1. intent expiry;
2. readiness decision;
3. SingleWriter leadership;
4. exchange/account/symbol/side `PositionKey` lock;
5. quantity and price filters;
6. risk approval and idempotent lifecycle submission;
7. transaction ledger, Fill-driven position projection and Outbox;
8. best-effort immediate event publication plus durable retry.

## Planner integration

`adapt_planner_intent()` accepts direction-four planner DTOs without importing the planning implementation. OPEN/INCREASE/REDUCE/CLOSE are the only execution actions. An UNSTUCK planner action is converted to REDUCE and retained as metadata.

## UNKNOWN recovery

`UnknownOrderResolver` merges direct order lookup, open orders, recent fills and User Stream cache. `UnknownOrderSupervisor` schedules retries with bounded exponential backoff and escalates unresolved orders to HALTED or MANUAL_REVIEW. No recovery path resubmits an order.

## Fake runtime

`build_integrated_fake_runtime()` creates an executable test system containing the execution engine, order service, position-aware fake exchange, double-leg account, transaction ledger, event publisher, User Stream cache, market rules, locks, SingleWriter, metrics, audit and action-group repository.

## Control plane

Read-only resources cover positions, pair summary, account risk, readiness, reconciliation, user-stream status, orders, action groups, events and audit. Write routes remain feature-disabled.
