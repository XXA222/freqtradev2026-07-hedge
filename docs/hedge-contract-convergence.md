# Hedge public-contract convergence boundary

`freqtrade/hedge/contracts/` is the only cross-direction contract authority.

The planner, execution state machine, simulator and persistence layer may keep private
models where their invariants differ, but conversions between directions must be made by
`freqtrade.hedge.contracts.adapters`. New direct conversions in services or composition
code are forbidden.

Current compatibility rule:

- Public `PositionSide`: `LONG`, `SHORT`.
- Public execution actions: `OPEN`, `INCREASE`, `REDUCE`, `CLOSE`.
- Planner-only `UNSTUCK` is normalized to public/execution `REDUCE`, while the original
  semantic remains in metadata.
- Public order identity, canonical symbol and side/action-to-BUY/SELL mapping are frozen.
- Private models are not public API exports and may only change with adapter tests.

This release centralizes the boundary and adds fail-fast enum compatibility checks. It does
not delete private internal models because doing so in one release would create a large,
high-risk migration unrelated to trading safety.
