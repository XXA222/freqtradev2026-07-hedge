# N7K2-R2.0 Durable Paper and PostgreSQL Authority

## Release boundary

N7K2-R2.0 advances the R1.8.4 FULL_PASS local baseline along five bounded paths:

1. Paper consumes the exact closed, analyzed DataProvider OHLCV row used for Hedge signals.
2. Maker/taker fees, funding settlements, and Paper account events are sourced and persisted explicitly.
3. `hedge.paper` is the canonical strongly typed simulation configuration; `hedge.simulation` is compatibility input only.
4. Execution order state and idempotency reservations are authoritative in SQL.
5. PostgreSQL 17 CI exercises migration, concurrent idempotency, Outbox, backup, and restore.

Real exchange writes remain locked. This release is not a Binance live-execution release.

## DataProvider OHLCV contract

Production Paper defaults to `ohlcv_source=dataprovider`. The signal and matcher receive the same analyzed candle and close timestamp. An unfinished, missing, malformed, or stale candle halts the Paper cycle. The automatic freshness budget is two timeframes when `candle_max_age_seconds=0`.

`ticker_compat` remains available only with `ephemeral=true`. A ticker may refine bid/ask spread, but it cannot replace production OHLCV or the planning mark.

## Fees, funding, and account events

Market precision, limits, and maker/taker fees are refreshed from the exchange adapter when available. Configured values are explicit fallbacks and the source is included in the market-rules version.

Funding history is preferred. A next announced settlement is retained and emitted only when a closed candle crosses its timestamp. REST polling is throttled; pending settlement evaluation is not. Funding events are keyed by symbol and settlement timestamp so a revised announced rate cannot create a second debit.

Paper fee and funding facts are stored as `hedge_account_events` and published through the transactional Outbox. Paper uses the `paper` exchange namespace and stores the venue exchange only as metadata, preventing real Binance facts from being replayed into the simulated wallet.

Production Paper requires funding and durable account-event recording. `funding_source=none` and `account_events_enabled=false` are test-only and require `ephemeral=true`.

## Strong configuration

`PaperSimulationConfig` validates exact decimals, booleans, integers, choices, limits, and production safety combinations. Legacy `hedge.simulation` values are merged into `hedge.paper`; conflicting legacy and canonical values fail closed; the legacy section is then removed from the normalized runtime config.

## SQL authority

`hedge_execution_order_states` stores the current execution lifecycle with immutable order identity and monotonic lifecycle versions. `hedge_execution_idempotency` stores leased IN_FLIGHT reservations and COMPLETED pointers to authoritative orders.

Order persistence and idempotency completion occur in one SQL transaction. Recovery can converge an older orphaned IN_FLIGHT lease when the authoritative order already exists. Paper restores SQL execution state even when no auxiliary checkpoint exists and filters by account, symbol, and Paper source.

The checkpoint remains an auxiliary cache for planner buckets, simulated account projections, and strategy state. It is no longer authoritative for execution order lifecycle or idempotency.

## PostgreSQL backup and restore

PostgreSQL restore validates schema and columns before mutation, derives parent-before-child insert order from foreign keys, rejects dependency cycles, truncates only the complete protected table set without CASCADE, restores transactionally, verifies row counts, and advances serial/identity sequences.

The CI workflow uses a disposable PostgreSQL 17 service and covers migration H3-027/H3-028, eight-worker reservation races, atomic completion/replay, account-event Outbox, backup, deletion, restore, and replay after restore.

## Evidence boundary

Local build evidence includes compileall, central integration verification, Stage A, P0, focused R2 tests, SQLite migrations, and the complete Hedge suite. PostgreSQL service execution and the complete Windows upstream suite remain external gates and must run in GitHub Actions and the user's verified Windows environment respectively.
