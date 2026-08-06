R5.1.4 fixes mainnet user-stream evidence timestamp parsing.

R2.5.3 readiness reports use completed_at_utc and started_at_utc. R5.1.3 did not
recognize completed_at_utc, so a passing timezone-aware report was rejected.
R5.1.4 explicitly accepts the canonical UTC evidence timestamp fields, ignores
optional null event timestamps, and still rejects naive, stale, or future evidence.
Mainnet order writes remain locked.
