# Freqtrade Hedge R4 Full Completion 4.0.1

R4.0.1 consolidates the remaining R3 work and hardens the R4 production-locked layer.

## Included

- Unified lifecycle for configuration, database, REST, user stream, reconciliation,
  planner, risk, execution, control plane and supervisor.
- Dependency-ordered fail-closed startup with reverse rollback.
- Strict configuration parsing: boolean, integer, symbol, namespace and finite caps.
- Shared core/tactical LONG and SHORT planner with runtime-enforced notional and gross caps.
- Independent planner execution on Backtest, Dry-run, Testnet and Production-Locked surfaces.
- Execution-handler accounting that never reports writes which did not occur.
- Duplicate candle/cycle protection and stale market/readiness blocking.
- Event replay checkpoint state parity and simulation invariants.
- Exact Testnet terminal-state validation with continue-after-failure matrix execution.
- Atomic metrics/status writes, query-string/Bearer redaction and aggregate diagnostics.
- Forty independent deep-audit checks covering security, lifecycle, planning, execution,
  simulation, Testnet, observability and packaging.

## Safety

- Mainnet exchange writes remain locked.
- Market orders are rejected.
- Only BTCUSDT and ETHUSDT USD-M perpetual aliases are accepted.
- The installer does not load credentials, connect to Binance or start a runtime.

## Diagnostic behavior

Every test section runs independently. A failed section is recorded and later sections
continue. The installer writes per-section stdout/stderr, readiness reports and one final
summary. It also creates a single upload ZIP in `C:\Users\<user>\Downloads`, so the full
result can be uploaded without manually locating individual files.
