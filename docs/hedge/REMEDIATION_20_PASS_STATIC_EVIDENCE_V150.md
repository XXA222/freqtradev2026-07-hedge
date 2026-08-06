# v1.5.0 Stage-A 20-pass static evidence

Generated: 2026-07-30T17:39:58.041117+00:00

> This is static/source evidence. It does not replace local full pytest, PostgreSQL, readonly soak or Binance testnet evidence.

| Pass | Engineering check | Result | Evidence |
|---:|---|---|---|
| 1 | Baseline and release boundary | PASS | 20-pass record and Stage-A boundary exist |
| 2 | Combined mode removed | PASS | combined rejected; shadow is explicit replacement |
| 3 | Source-separated projections | PASS | runtime has four source namespaces and independent projection map |
| 4 | Effective safety priority | PASS | paper cannot clear exchange/system halt; effective source mode policy present |
| 5 | Native migration isolation | PASS | upstream check_migrate has no H3 import/call/recovery symbol |
| 6 | Explicit H3 bootstrap and CLI | PASS | feature-gated plan/migrate/verify surface present |
| 7 | Common lifecycle retained | PASS | market/DataProvider/strategy common phases retained before hedge controller |
| 8 | Native write barrier retained | PASS | native exchange writes and exit cleanup remain blocked in Hedge mode |
| 9 | Strict configuration | PASS | unknown Hedge/Planner/Paper keys fail closed |
| 10 | Exchange market rules | PASS | market precision/minimums prefer loaded exchange metadata |
| 11 | Strategy signal adapter | PASS | latest analyzed DataFrame is adapted to typed signal snapshot |
| 12 | Conservative Paper matcher | PASS | price-touch/partial-fill/fill-limit/fee semantics are wired |
| 13 | Durable Paper checkpoint | PASS | source-scoped SQL checkpoint with versioned state exists |
| 14 | Durable execution facts | PASS | intent/fill/audit/outbox persisted before in-process advance |
| 15 | Transactional risk commit | PASS | unique SQL risk evidence store fails closed on conflicting decision |
| 16 | Recoverable action groups | PASS | multi-leg action group state has SQL repository |
| 17 | WS replay and GAP | PASS | bounded sequence history, replay cursor and explicit gap event present |
| 18 | RBAC and scoped confirmation | PASS | server-side role mapping and payload/account/symbol-bound confirmations present |
| 19 | Legacy readonly deprecation | PASS | legacy façade warns and new exchange/readonly stack remains canonical |
| 20 | Release safety gates and live lock | PASS | live network writes remain disabled; compileall and git diff --check pass |

## Aggregate

- Checks passed: 20/20
- Python compileall: PASS
- git diff --check from v1.4.2 baseline: PASS
- Live Binance writes: LOCKED
- External/runtime evidence: required before promotion
