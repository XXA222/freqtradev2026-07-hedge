# R5.2 Production Strategy Core

R5.2 connects the existing deterministic R4 hedge planner to a production-equivalent,
durable strategy runtime. It implements the original project objective rather than a single
exchange endpoint milestone.

## Functional closure

- Independent LONG and SHORT planning on the same USD-M symbol in Hedge Mode.
- Core inventory and tactical inventory kept separately.
- Initial entry, bounded grid additions, trailing-confirmed additions, layered profit taking,
  partial reductions, Unstuck, cooldowns, wallet exposure limits, funding policy and
  volatility-adjusted grid spacing through the shared R4 planner.
- Identical planner contract for SHADOW, DRY_RUN, TESTNET, MAINNET_LOCKED and
  MAINNET_LIMITED.
- LIMIT-only execution. MARKET orders are structurally rejected.
- Risk-reducing submissions are ordered before risk-increasing submissions.
- The complete plan is committed to SQLite before the first exchange write.
- UNKNOWN submit or cancel status moves the cycle to RECOVERY_REQUIRED; it is never blindly
  submitted again.
- Only orders recorded in the managed-order registry can be canceled.
- Existing positions require a dual-signature adoption manifest before any live strategy
  writes. Shadow mode protects unadopted inventory as core.
- Daily order, turnover, loss, gross, net and open-order limits are enforced by the R5 budget
  store. An entire batch is reserved atomically before the first write.
- MAINNET_LIMITED additionally requires a matching stage permit and completed previous-stage
  evidence from the R5 journal.

## Durable data

`R5StrategyStateStore` uses SQLite WAL and `BEGIN IMMEDIATE` transactions for:

- cycle input identity, plan, state and final report;
- immutable cycle events;
- LONG/SHORT strategy state;
- adopted core baselines;
- managed exchange orders;
- core/tactical inventory legs;
- idempotent fill and funding events.

`R5InventoryProcessor` projects exchange fills into the core/tactical ledger exactly once.
Tactical lots retain independent entry price, layer, fees, funding and realized PnL.

## Runtime modes

| Mode | Planner | Durable plan | Exchange writes |
|---|---:|---:|---:|
| SHADOW | yes | yes | no |
| DRY_RUN | yes | yes | no |
| TESTNET | yes | yes | explicit executor only |
| MAINNET_LOCKED | yes | yes | no |
| MAINNET_LIMITED | yes | yes | executor + budget + permit + promotion evidence |

## Current mainnet safety boundary

The delivered OneShot installer and optional mainnet shadow runner do not submit, test, cancel,
or modify exchange orders. The shadow runner performs account reads and produces plans for
BTCUSDT and ETHUSDT while `exchange_write=LOCKED`.

Existing ETHUSDT positions and orders are observed but not adopted, canceled, reduced or
modified automatically. Live adoption remains a separate dual-control operation.
