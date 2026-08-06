# R3.4 production-equivalent Dry-run E2E

R3.4 removes the second temporary Fake main-loop from `HEDGE_SIMULATED` and makes
`IntegratedPaperHedgeApplication` the single authoritative candle-cycle owner.

## Runtime ownership

The same durable Paper execution runtime is shared by:

- the Freqtrade analyzed-candle lifecycle;
- planner, risk approval and execution state machine;
- partial-fill and conservative OHLC matching;
- SQL execution store, idempotency and recovery;
- Kill Switch and stop-new-risk state;
- control-plane cancel and close operations;
- Paper account and position views.

This prevents a candle from being processed once by the Paper application and a
second time by the R3.1 production-equivalent loop.

## Control-plane behavior

In `HEDGE_SIMULATED` mode, control operations use the Paper wallet rather than the
real Binance account projection. `CLOSE_LONG`, `CLOSE_SHORT`, and `CLOSE_BOTH`
submit reduce-only simulated orders into the same execution store used by the
Paper cycle. These orders become eligible for the existing conservative matcher
on the next closed candle.

`STOP_NEW_ORDERS` is bound to the Paper submission gate. Risk-increasing planner
orders are removed before execution while reduce-only orders and cancellations
remain available. Planner temporal state for a blocked side does not advance for
an order that was not submitted.

## Supported modes

- `paper + HEDGE_SIMULATED`: local durable Dry-run without private Binance account data.
- `shadow + HEDGE_SIMULATED`: Binance read-only projection plus durable Paper execution.
- `readonly/shadow + HEDGE_PRODUCTION_LOCKED`: unchanged; real order writes stay locked.

## Safety

R3.4 does not enable `HEDGE_PRODUCTION_ARMED`, load Binance credentials, or send
exchange order requests. Production exchange writes remain governed by the R3.0
fail-closed gate.
