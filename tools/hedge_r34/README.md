# R3.4 Production-equivalent Dry-run E2E

R3.4 makes the durable Paper application the single authoritative cycle owner for
`HEDGE_SIMULATED`. The control plane, execution store, kill switch, account view,
and Freqtrade candle loop all share that same runtime. Production exchange writes
remain locked.
