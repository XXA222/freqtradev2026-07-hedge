# Freqtrade-Hedge R3.3.1 Control Plane

R3.3.1 adds a production control plane around the R3.2 main program integration.

Core behavior:

- Read-only control status.
- RBAC roles: VIEWER, OPERATOR, RISK_MANAGER, ADMIN.
- One-time scoped confirmations for dangerous operations.
- Durable SQL idempotency and operation results.
- Stop/resume new risk.
- Kill-switch activate/release.
- Cancel managed orders only.
- Close LONG, SHORT, or both legs.
- BTCUSDT and ETHUSDT perpetual allowlist only.
- Production-locked actions return deterministic plans with zero exchange writes.

The R3.3.1 installer and acceptance runner do not load Binance credentials and do not
send exchange order requests.

Windows SQLite acceptance cleanup uses NullPool and explicit engine disposal.
