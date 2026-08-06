# N7K2-R1.7 native migration compatibility

Windows full-Hedge collection exposed one remaining released API mismatch:
`tests/hedge/test_persistence.py` imports `migrate_hedge_position_columns` from
`freqtrade.persistence.migrations`, while the implementation had been moved to
the explicit Hedge bootstrap module.

R1.7 restores a thin wrapper at the historical import location. The wrapper:

- delegates to `migrate_hedge_compatibility_columns`;
- adds only `trades.position_side` and the three released Order audit columns;
- is idempotent;
- backfills legacy rows to `BOTH`;
- never runs H3 ledger migrations;
- is not invoked by native `check_migrate()`.

This keeps older callers working without weakening ordinary Freqtrade database
isolation.
