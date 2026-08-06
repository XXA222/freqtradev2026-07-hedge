# P1B Position-Side Persistence Scaffold

This stage adds backward-compatible side identity to persisted trades and orders.

- Legacy rows default to `BOTH`.
- Hedge rows may use `LONG` or `SHORT`.
- Orders additionally carry `position_action` and `action_group_id`.
- Migration is additive and idempotent.
- Global trade-opening and exchange-order behavior remain locked.

This stage does not enable hedge execution.
