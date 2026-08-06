
# P1 Core Data Plane

This milestone completes a larger read-only foundation for hedge mode:

- Trade and Order rows persist `position_side` and audit action metadata.
- Legacy rows migrate idempotently to `BOTH`.
- Backtesting keeps a parallel `(pair, position_side)` open-trade index.
- Side-aware trade queries allow LONG and SHORT to coexist for one pair.
- Wallets keep a parallel `(symbol, side)` position index while preserving the legacy API.
- A side-aware position book calculates net position, gross exposure, and hedge ratio.
- Read-only reconciliation detects missing legs and amount drift.

Execution, automatic mode switching, and exchange order mapping remain disabled.

## Patch idempotency safeguard

The interrupted P1 patch exposed an idempotency defect: a multiline source
marker was checked with a single-line substring, allowing the Order restoration
metadata arguments to be inserted twice on a repeated patch run. The repair
normalizes the source to one metadata block and adds a source-integrity unit
test. Future milestone delivery must pass this test before commit.

## LocalTrade structural boundary restoration

The first side-query patch inserted an unindented block immediately before the
original `LocalTrade.get_open_trades` method. That dedent ended `LocalTrade`
early. The original `get_open_trades`, `get_open_trade_count`, `from_json`, and
the intervening methods then became nested definitions inside the final
module-level hedge helper. The file remained syntactically valid, which is why
compile checks did not expose the structural damage.

The repair removes only the malformed inserted prefix, preserving the original
indented upstream methods. It then verifies that the upstream methods are again
members of `LocalTrade`, inserts the canonical hedge methods at the restored
`get_open_trades` boundary, and adds AST regression tests covering both original
and added method ownership.

## Fixed-container validation

Development now uses a fixed, non-trading container named
`freqtrade-hedge-dev`, based on the existing
`freqtrade-hedge:p1-core-data-plane-v8-test` image. The current workspace is
bind-mounted at `/workspace` and commands run as `ftuser`, which keeps the
runtime and test dependencies visible.

The focused P1 regression suite passes 394 tests plus 3 subtests. A separate
fixed baseline container named `freqtrade-hedge-runtime`, based on the accepted
`p1-domain-config` image, also passes the Binance ETH/USDT futures market check
and reproduces the one-way baseline backtest: 31 trades, -3.76% total profit,
64.5% win rate, and 7.28% maximum drawdown.

After the compatibility repairs, the core upstream suite passes 4359 tests,
with 20 skips, 395 deselections, and no failures. The deselected scope is
formally isolated from the P1 acceptance boundary:

- `tests/freqai` requires the optional Torch/RL environment. Lightweight model
  discovery is covered, but the complete FreqAI stack is not installed in the
  fixed development container. The manually added CPU `xgboost` package also
  advertises an optional CUDA NCCL dependency, so `pip check` is not used as a
  core P1 gate for this container.
- `tests/test_pip_audit.py` performs a live vulnerability audit that may submit
  the installed dependency inventory to an external service. It is excluded
  until that disclosure is explicitly approved or an offline audit database is
  provided.
- `test_list_timeframes` reads the real bind-mounted futures configuration when
  run from `/workspace`. It passes in a clean working directory and is excluded
  only from the workspace-wide run to prevent configuration pollution.

These exclusions affect optional tooling and test isolation, not the hedge
data plane. P1 acceptance is therefore complete, while exchange order mapping,
automatic position-mode switching, and hedge execution remain disabled.
