# Freqtrade-Hedge v1.5.0 Stage-A N7K2-R1 deep optimization

## Safety boundary

This release remains **read-only / Paper only**. Binance write execution stays hard-gated.
No production-write, testnet, PostgreSQL CI, or 24/72-hour soak claim is made by this package.

## Corrected defects

1. Removed obsolete native migration assignments which derived `LONG`/`SHORT` from `is_short`
   and assigned `hedge_version=1` before being shadowed by the v1.5 compatibility block.
2. Native table rebuild now has one authoritative rule: preserve existing explicit Hedge facts;
   otherwise copy an ordinary row as `position_side=BOTH`, `hedge_version=0`, and
   `open_slot_key=NULL`.
3. Replaced unsafe central-source semantic mutation with fail-closed verification.
   `integrate_h3_full.py` no longer injects fields into an arbitrary upstream `trade_model.py`.
   Central integration must use the hash-verified release overlay.
4. Hardened the installer against ZIP path traversal, duplicate manifest entries, renamed browser
   downloads such as `(1).zip`, hard-coded Python paths, absent Git metadata, and partial failure.
5. Validation now separates mandatory offline-capable source checks from optional dependency,
   vulnerability, full-Hedge, and full-upstream suites.

## Validation performed during packaging

- Python `compileall`: passed for `freqtrade`, `tests/hedge`, `scripts`, `tools`, and integrator.
- Focused domain tests: 115 passed.
- Exchange: 74 passed.
- Concurrency: 30 passed.
- Planning: 34 passed.
- Readiness: 11 passed.
- Read-only runtime: 56 passed.
- Simulation: 29 passed.
- Telemetry: 36 passed.
- SQLite H3 migration harness: four legacy order schemas passed; all 26 migrations applied,
  second run skipped all 26, and no migration remained pending.

The packaging environment did not contain every optional Freqtrade dependency, so the complete
upstream test suite was not claimed here. The PowerShell installer can install/synchronize those
requirements and run the complete suites on the target Windows environment.
