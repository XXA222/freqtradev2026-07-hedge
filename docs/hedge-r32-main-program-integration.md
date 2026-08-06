# Hedge R3.2 Main Program Integration

R3.2 binds the production-equivalent R3.1 main loop to Freqtrade's common
post-analysis cycle. It consumes the authoritative Binance read-only account
view and one analyzed closed candle, builds a conservative PlanningContext,
and executes the configured locked or simulated loop.

Safety rules:

- Only BTCUSDT and ETHUSDT perpetual symbols are accepted.
- Existing exchange positions are classified as protected core exposure.
- Non-managed symbols are filtered before planner normalization.
- REST, user-stream, clock, configuration, and reconciliation health are
  evaluated before planner execution.
- A closed candle is processed once.
- Account identity must match the main-loop account identity.
- HEDGE_PRODUCTION_ARMED is not supported by R3.2.
- HEDGE_PRODUCTION_LOCKED performs zero exchange writes.
