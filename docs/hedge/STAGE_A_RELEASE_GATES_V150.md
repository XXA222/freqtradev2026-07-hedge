# Stage-A release gates — v1.5.0

A release is `PASS` only when all local gates pass:

- compileall and changed-file Ruff;
- Stage-A remediation regressions;
- complete `tests/hedge` excluding installer self-tests;
- complete upstream pytest;
- pip dependency and vulnerability checks;
- `git diff --check`;
- runtime configuration restored after test isolation;
- exchange writes remain locked.

External gates are reported separately and cannot be inferred from unit tests:

- SQLite migration/restore fault matrix;
- PostgreSQL migration, concurrent lease, Outbox and restore matrix;
- Binance readonly 24-hour then 72-hour soak;
- Binance futures testnet lifecycle and UNKNOWN recovery;
- live RBAC/confirmation/Kill-Switch drill.

Until those are attached to a release, capability is limited to readonly and durable Paper/Shadow.
