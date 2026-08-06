# Freqtrade Hedge v1.4.1 local safety hotfix

This hotfix corrects two delivery defects in v1.4.0 K8N4 without adding strategy features.

- `Trade.__init__()` now normalizes explicit LONG/SHORT Hedge identity immediately.
- Explicit Hedge trades therefore receive `hedge_version >= 2` and an active `open_slot_key` before serialization.
- Native `BOTH` trades remain upstream-compatible and do not emit Hedge JSON fields.
- The repair installer uses scoped whitespace checks and a rollback loop compatible with Windows PowerShell 5.1.

The live exchange write gate remains locked.
