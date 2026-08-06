# Freqtrade-Hedge R3.0 Production Execution Core

R3.0 completes the main production-capable order execution graph while keeping live
exchange order writes locked by default. The scope is Binance USD-M perpetual contracts
for the explicit BTCUSDT and ETHUSDT allowlist.

Included core capabilities:

- fail-closed live/testnet production gate with process-local two-step arming;
- concrete Binance USD-M hedge-mode order adapter;
- positionSide-aware LONG and SHORT order mapping;
- idempotent submit and durable lifecycle integration;
- partial fill and terminal state handling;
- ambiguous write outcome to UNKNOWN followed by query-based recovery;
- cancel, cancel-replace and close-both action groups;
- user-stream ORDER_TRADE_UPDATE bridge;
- Kill Switch behavior which blocks increases while allowing reductions/cancellations;
- production composition root using explicit risk, persistence, readiness and lock ports;
- readiness audit based on R2.4.1 and R2.5 evidence.

The R3.0 runner does not submit an order. It installs the code, runs isolated and targeted
regression tests, and emits a CORE_COMPLETE_LIVE_LOCKED report.
