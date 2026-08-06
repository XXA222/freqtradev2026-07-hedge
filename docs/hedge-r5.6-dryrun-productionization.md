# R5.6 Dry-run Productionization

R5.6 extends the R5.5 local dashboard and strategy contract with an operational safety layer. Enable it only under `dry_run=true`.

```json
"hedge": {
  "read_only": true,
  "live_trading_enabled": false,
  "r56": {
    "enabled": true,
    "state_path": "user_data/hedge/r56/runtime-state.json",
    "market_max_age_seconds": 90,
    "warmup_candles": 100,
    "max_gross_ratio": "0.60",
    "max_margin_ratio": "0.45",
    "max_net_ratio": "0.35",
    "drawdown_warning": "0.05",
    "drawdown_pause": "0.10",
    "drawdown_kill": "0.20",
    "drawdown_recovery": "0.03"
  }
}
```

Before the first valid R5.6 observation, new risk is blocked. Failures in state recovery, market freshness, warmup, risk limits or the drawdown breaker also block new risk. Reduce-only exits remain available through the existing planner/execution path.

Start locally with:

```powershell
.\scripts\Run-Freqtrade-Hedge-R5.6-DryRun-Operations.ps1 -OpenDashboard
```

Offline acceptance:

```powershell
python -m tools.hedge_r560.r560_toolkit.offline_acceptance --project-root .
```
