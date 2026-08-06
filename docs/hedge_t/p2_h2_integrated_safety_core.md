# P2-H2 Integrated Safety Core v2.0

## Implemented

- finite Decimal validation;
- canonical symbol matching;
- account-aware LONG/SHORT identity;
- immutable action-plan grouping;
- target-position delta and deterministic idempotency key;
- local Reduce-Only calculation;
- account/side-aware position book and explicit short-only hedge ratio;
- finite nonnegative reconciliation tolerance;
- explicit notional versus exposure-ratio risk limits;
- full-account Binance position/order/fill reads;
- unmanaged position/order HALT;
- side/sign validation and clock-skew checks;
- Wallet legacy projection fail-closed when two legs exist;
- P2 configuration and execution lock;
- listen-key lifecycle with mandatory REST recalibration;
- focused tests and Docker full-suite gates.

## Explicitly not implemented

- database Intent/Fill/Snapshot/Outbox tables;
- automatic database migration;
- production WebSocket transport;
- order submission or cancellation;
- leverage, margin-mode or position-mode writes;
- P3 execution service;
- double-leg backtesting;
- UI or message-control write endpoints.

The P3 write path remains locked.

## Validation record (2026-07-26)

- `tests/hedge`: 125 passed, 3 subtests passed;
- `tests/hedge/p2_h2`: 35 passed;
- `ruff check freqtrade/hedge tests/hedge`: passed;
- `python -m compileall -q freqtrade/hedge`: passed.

`tests/commands/test_startup_time.py::test_startup_time` is formally isolated
from the Docker functional gate. It measures a 0.5-second upstream CLI startup
budget, not Hedge behavior. Three consecutive Windows bind-mount measurements
were 0.759s, 0.775s, and 0.640s, while the earlier P2-H1 image baseline passed.
The functional full-suite command therefore deselects this environmental
performance probe; it remains reported rather than hidden and must be re-run
against a Linux-native CI filesystem before any upstream performance claim.
