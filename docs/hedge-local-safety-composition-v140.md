# Freqtrade-Hedge local safety and composition hardening v1.4.0

## Scope

This release adds no strategy signals or trading features. It closes four P0 safety defects
and assembles the already implemented persistence, readiness, fencing, risk, Paper matcher
and control-plane components through one composition root.

## P0 changes

1. Native Freqtrade write paths are blocked at configuration, bot service, RPC and Exchange
   instance boundaries whenever `hedge_mode_enabled=true`.
2. Native Trade/Order JSON schemas remain unchanged for ordinary entities. `BOTH` or null
   wallet sides fall back to the upstream long/short trade direction.
3. Risk/readiness identity is hosted at `freqtrade.hedge.identity`, removing the cold-import
   cycle.
4. `hedge.operation_mode=combined` is rejected until real and simulated views are isolated.

## Composition changes

`freqtrade.hedge.integration.composition` is the sole production assembly point. Paper mode
requires a database-backed single-writer lease from the initialized SQLAlchemy engine, the real
ReadinessGate, PositionLockManager and HedgeRiskRuntime. Read-only mode requires a durable
persistence service and never falls back to an in-memory repository.

Paper/Dry-run fills now use `ConservativeMatcher`, the same deterministic OHLC/liquidity
model used by backtesting. Confirmed account, core/tactical and planner state is saved to an
atomic JSON state document. Pending orders are intentionally not resurrected after restart;
the next planner cycle recreates the ideal set from confirmed state.

## Contract convergence

Cross-direction conversion now lives in `freqtrade.hedge.contracts.adapters`. Private DTOs
remain internal where their invariants differ, but direct service-to-service conversions are
no longer the supported boundary.

## Remaining gates

- Real Binance write execution remains locked.
- Combined mode remains disabled.
- PostgreSQL migration and real Binance read-only soak tests still require their external
  environments.
- A clean Git worktree is required before running `scripts/New-Hedge-Source-Checkpoint.ps1`.
