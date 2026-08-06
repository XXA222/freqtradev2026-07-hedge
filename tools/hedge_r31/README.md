# R3.1 Production-Equivalent Main Loop

This layer connects the existing deterministic planner to the R3.0 execution engine.
It supports disabled, simulated, production-locked and production-armed modes while
keeping BTCUSDT and ETHUSDT as the only supported USD-M perpetual symbols.

Key safety properties:

- Durable ExecutionStore membership is authoritative for order ownership.
- External and orphan-prefix orders are never canceled.
- New risk is blocked on a side containing an external active order.
- Planner idempotency is stable across repeated cycles.
- Recovery queries existing orders and never blindly resubmits.
- Kill Switch cancels only authoritative managed orders.
- Strategy state is committed only after cycle actions complete without errors.
- The R3.1 acceptance runner does not read credentials or connect to Binance.
