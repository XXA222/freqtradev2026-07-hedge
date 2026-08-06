# N7K2-R2.1 Local Backtest and Durable Paper Runtime

## Release boundary

R2.1 targets two executable capabilities:

1. local dual-leg Hedge backtesting through Freqtrade's normal configuration, strategy and historical-data stack;
2. real-market-data, SQL-durable Paper simulation without exchange writes.

It does **not** enable Binance order submission. `live_trading_enabled` remains false and the write adapter remains hard-gated.

## No-lookahead execution contract

The analyzed candle closes first. Its indicators and Hedge signal may create or replace ideal orders at that close, but those orders are not eligible for matching against that candle's open/high/low/close path. Existing orders are matched first; newly planned orders become eligible on the next closed candle.

Backtest and Paper use the same ordering:

1. accept the current signal;
2. apply funding events due at that timestamp;
3. match orders that existed before the current bar;
4. apply fills, fees, liquidation and expiry;
5. plan/cancel/submit the next ideal order set;
6. persist projections and the candle cursor.

Duplicate closed candles are skipped by the controller. Out-of-order candles fail closed. At least two analyzed candles are required for a meaningful backtest.

## Data and signal sources

- OHLCV comes from Freqtrade DataProvider or downloaded historical data.
- Ticker data may refine bid/ask only; it cannot synthesize the production Paper candle.
- Preferred strategy columns are `hedge_long_score`, `hedge_short_score`, `hedge_target_net` and `hedge_model_version`.
- Existing `enter_long` and `enter_short` columns remain compatible.
- Futures backtesting configured with exchange funding requires downloaded funding/mark data and fails closed when it is absent.
- Market precision, minimums and maker/taker fees are read from the resolved exchange markets with explicit fallback provenance.

## Included local assets

- `config_examples/strategies/HedgeDualEmaExample.py`
- `config_examples/config_hedge_backtest.example.json`
- `config_examples/config_hedge_paper.example.json`
- `scripts/Prepare-Hedge-Data-R2.1.ps1`
- `scripts/Run-Hedge-Backtest-R2.1.ps1`
- `scripts/Run-Hedge-Paper-R2.1.ps1`
- `scripts/Validate-Hedge-Local-R2.1.ps1`

## Windows workflow

From PowerShell:

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Prepare-Hedge-Data-R2.1.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge" -Timerange "20260101-20260801"
```

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Run-Hedge-Backtest-R2.1.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge" -Timerange "20260101-20260801"
```

The backtest writes a deterministic JSON artifact to `user_data/backtest_results/hedge-r2.1.json`. Add `--hedge-export-events` to the CLI command when the complete event ledger is needed; this can create a large file.

Before starting continuous Paper:

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Validate-Hedge-Local-R2.1.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge"
```

Then:

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Run-Hedge-Paper-R2.1.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge"
```

## Durable Paper requirements

The `hedge-paper` command rejects unsafe production-like configurations. Non-ephemeral Paper requires:

- `dry_run=true`;
- `hedge.operation_mode=paper`;
- `read_only=true` and `live_trading_enabled=false`;
- file-backed SQLite or PostgreSQL;
- `hedge.paper.state_backend=sql`;
- `ohlcv_source=dataprovider`;
- `funding_source=exchange`;
- durable account events enabled.

SQL execution order and idempotency projections are authoritative. The auxiliary checkpoint remains responsible only for planner, bucket and simulated-account state that has not yet been migrated to immutable event reconstruction.

## Result interpretation

The JSON result records:

- `execution_timing=NEXT_BAR_NO_LOOKAHEAD`;
- pair, timeframe, data interval and strategy version;
- market-rule source/version;
- bar, signal and funding counts;
- equity, realized PnL, funding, fees, gross risk and final LONG/SHORT quantities;
- optional complete event ledger.

This is a simulation result, not evidence of executable live profitability. Conservative fill assumptions, historical data quality and exchange funding coverage must be reviewed before strategy conclusions are accepted.

## Remaining production gaps

R2.1 is intended to reach local backtest and durable Paper usability. Production live readiness still requires:

- PostgreSQL workflow execution in a real CI service and restore evidence;
- 24/72-hour Binance read-only and Paper soak tests;
- process-kill, DB restart, network partition and stale-data chaos tests;
- elimination or accepted baseline governance for legacy Ruff debt;
- persistent WebSocket replay/gap handling and operator observability;
- real RBAC, confirmation tokens and audited operational actions;
- a Binance testnet write adapter with idempotent UNKNOWN recovery;
- controlled live gates, kill-switch rehearsal and rollback evidence.
