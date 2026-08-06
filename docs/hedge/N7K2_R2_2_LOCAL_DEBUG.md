# R2.2 Local Debug Runbook

## 1. Offline deterministic debug

This is the first command after installation. It does not use the network or exchange credentials.

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Debug-Hedge-Local-R2.2.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge"
```

Expected final lines:

```text
========== R2.2 LOCAL DEBUG COMPLETE ==========
Status: PASS
Exchange writes: LOCKED
```

The audit folder is created under `user_data\audit\r2.2-local-debug-*` and contains the SQLite database, JSON result and combined command log.

## 2. Typed configuration validation

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Validate-Hedge-Local-R2.2.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge"
```

This validates both example configurations and reruns the offline durable debug.

## 3. Historical data

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Prepare-Hedge-Data-R2.2.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge" -Timerange "20260101-20260801" -Timeframes "5m"
```

Network access and Binance public-data availability are required. No private API key is required for public historical data.

## 4. Local Hedge backtest

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Run-Hedge-Backtest-R2.2.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge" -Timerange "20260101-20260801"
```

Review the generated result JSON and its `.sha256` sidecar before running Paper.

## 5. Durable Paper

```powershell
& "D:\Program Files\freqtrade-hedge\scripts\Run-Hedge-Paper-R2.2.ps1" -ProjectRoot "D:\Program Files\freqtrade-hedge"
```

Paper requires public market access and configured exchange read capability. It remains dry-run/read-only and cannot write orders to Binance.
