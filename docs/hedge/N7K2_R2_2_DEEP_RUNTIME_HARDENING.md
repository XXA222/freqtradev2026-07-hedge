# N7K2-R2.2 Deep Runtime Hardening

## Release boundary

R2.2 is a local-backtest and durable-Paper hardening release. It does not enable Binance writes and it is not a production-live release. Its purpose is to make the already available R2.1 local workflow fail closed under missed candles, revised candles, process interruption, duplicate exchange events, checkpoint failures, and incomplete recovery.

## Correctness changes

### Durable candle cursor

Paper now stores the last accepted closed bar and its deterministic fingerprint. On restart the signal provider returns every unseen closed bar after that cursor instead of processing only the newest row. The runtime:

- rejects a revised already-committed candle when `reject_revised_candle=true`;
- rejects missing candle gaps beyond `max_missing_candles`;
- caps catch-up work with `max_catchup_candles`;
- ignores the current unfinished candle;
- never uses a current ticker to enrich historical catch-up bars;
- advances the durable cursor only after the auxiliary checkpoint is committed.

A failed checkpoint poisons the current runtime instance. The process must restart and recover rather than continue from a state whose SQL facts and auxiliary planner state may have diverged.

### SQL fill recovery

Order lifecycle and idempotency remain SQL-authoritative. R2.2 additionally rebuilds simulated positions, core/tactical bucket allocation, cumulative fees, and missing fee account events from immutable SQL fill facts. The checkpoint is treated as a cache for planner and account projections, not as the only source of executed fills.

Recovery is scoped by account, symbol, and Paper source. Duplicate trade identifiers must be semantically identical; conflicting duplicates fail closed. Fill metadata now preserves the last fill fee and fee currency across execution snapshots and checkpoints.

### Funding rollback

Funding provider pending settlements, polling cursor, and emitted settlement identities can be snapshotted and restored. If a Paper business cycle fails before its checkpoint is committed, in-memory funding state rolls back with the rest of the cycle instead of silently consuming a settlement.

### Deterministic backtest evidence

Hedge backtest output now contains:

- an OHLCV data fingerprint;
- a semantic result fingerprint that excludes wall-clock creation time;
- an artifact SHA256 sidecar;
- atomic artifact replacement.

The runtime rejects missing historical bars according to the configured gap policy, so two executions cannot silently compare different input sequences as if they were the same run.

### Local safety preflight

The local Paper command validates managed pair membership, wallet currency, leverage, capacity, durable database URL, strict decimal values, and production-safe Paper configuration before a worker is started.

## Local validation

Run the self-contained offline durable smoke first. It performs no network call and uses no exchange credential:

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Debug-Hedge-Local-R2.2.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge"
```

The smoke proves:

1. the first closed candle can submit an order but cannot fill it on the same candle;
2. the second closed candle can fill the previously accepted order;
3. SQL order, idempotency and fill facts survive object destruction;
4. a fresh runtime rebuilds the candle cursor, position, bucket allocation, balance and fees;
5. a third candle can continue processing without duplicate fill application.

After that, validate real local assets:

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Validate-Hedge-Local-R2.2.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge"
```

Then prepare data, run backtesting and only after reviewing the result start durable Paper.

## External gates not claimed by this release

- Windows full upstream test result must be produced by the OneShot installer on the target machine.
- PostgreSQL 17 migration/concurrency/Outbox/backup/restore must pass in a real service CI job.
- Binance REST/User Stream and funding collection require a 24/72-hour read-only or Paper soak.
- Exchange writes remain locked; testnet/live execution is outside this release.
