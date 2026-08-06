# R3.8.1 Production Runtime Closed Loop

R3.8.1 retains R3.8 and adds a Windows lock-contention correction to an external fail-closed process supervisor around the existing Hedge-enabled Freqtrade process.

## Guarantees

- one supervisor per state directory using an operating-system file lock;
- R3.7 security report validation before child startup;
- optional SQLite backup through the SQLite backup API before every start;
- atomic lifecycle and heartbeat state;
- bounded exponential restart policy;
- graceful stop request followed by bounded force termination;
- JSONL supervisor events with recursive secret redaction;
- child process launched with `shell=False` and a sanitized environment;
- only `HEDGE_SIMULATED`, `HEDGE_PRODUCTION_LOCKED`, and `HEDGE_TESTNET_VALIDATION` deployment modes;
- no production-armed mode in R3.8.

The installer validates and installs the supervisor but never starts Freqtrade, loads credentials, connects to Binance, or sends orders.

## R3.8.1 Windows lock correction

The lock file is seeded before acquisition. A contender locks byte zero before reading or writing identity data, so a sharing violation becomes InstanceLockError rather than PermissionError.
