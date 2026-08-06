# P2 Binance Read-only

P2 starts with an isolated Binance futures account reader. It does not reuse
the normal Freqtrade position method because that method intentionally returns
no account positions in dry-run mode, and normal Binance initialization rejects
accounts that are already in dual-side position mode.

The read-only adapter currently:

- reads `dualSidePosition` without changing it;
- reads CCXT futures positions for an optional managed symbol;
- normalizes Binance `LONG`, `SHORT`, and signed one-way positions into P1
  `PositionRecord` values;
- filters zero-sized position rows; and
- rejects non-Binance or writable runtime configuration.

The account check now feeds the managed-pair snapshot into P1 reconciliation.
It reports a configuration/account position-mode mismatch, rejects malformed
mode responses instead of guessing, and flags any nonzero position returned
outside the configured managed pair. Its final consistency flag requires both
the safety checks and the amount reconciliation to pass.

Private API failures are classified without exposing credentials or response
payloads: rate limits become `DDosProtection`, transient exchange failures
become `TemporaryError`, and authentication or permission failures become
non-retryable `OperationalException` errors. Non-object mode responses,
non-list position responses, non-object position rows, and invalid numeric
amounts fail closed rather than being interpreted as an empty account.

Each account check can produce a structured audit report containing its UTC
observation time, configured and observed modes, normalized position amounts,
and safety/reconciliation issues. It deliberately omits the CCXT API object,
credentials, and raw private API payload. Tests inject credential-like values
into both the API mock and raw position data and verify that none reach the
report.

The adapter intentionally exposes no position-mode switch, leverage mutation,
margin mutation, or order API. Unit tests use a mock private API so they cannot
change an exchange account. Validation against a credentialled Binance account
remains pending and must use read-only API credentials.

The offline position/reconciliation core is complete. The v3.1 project archive
defines a wider P2 boundary that also includes account risk fields, user-data
stream synchronization, periodic REST calibration, HALT behavior, and zero
drift across multiple funding cycles. P2 therefore remains in progress until
those remaining items are implemented and observed. Live private API
validation is an environment acceptance task rather than authorization to add
credentials or enable hedge execution.

## Account risk snapshot and HALT

The adapter now reads wallet balance, equity, available balance, initial
margin, maintenance margin, and derives the maintenance-margin ratio. Missing,
invalid, negative, or zero-equity account values fail closed.

The combined assessment enters `HALT` for position/configuration drift or an
impossible margin state. `allows_risk_increase` is false in HALT and true only
when both reconciliation and account checks are consistent. This is a
read-only decision flag; it does not submit corrective orders.

## User-data event normalization

The first user-stream increment adds a pure parser for Binance USD-M
`ACCOUNT_UPDATE` events. It validates event and transaction timestamps,
balance rows, update reason, and explicit LONG/SHORT position rows, filters to
the configured managed pair, and converts changed legs into `PositionRecord`
values. Unexpected event types, malformed arrays, ambiguous `BOTH` sides, and
invalid numeric values fail closed.

The read-only stream mirror must be bootstrapped from a complete REST position
snapshot before it accepts events. It merges only the legs included in each
partial update, removes a leg when Binance reports zero quantity, and preserves
the untouched leg.

The mirror enters HALT for out-of-order events, stream disconnects, stale event
age, nonzero positions outside the managed pair, or a mismatch against a later
REST position calibration. It never submits a corrective order. A live
WebSocket connection, automatic reconnect/bootstrap, and the periodic
calibration scheduler remain pending.

The mirror also accepts normalized Binance USD-M `ORDER_TRADE_UPDATE` events.
It records the order ID, client order ID, explicit LONG/SHORT leg, execution
type, status, original quantity, cumulative and last fill quantities, average
price, and realized profit. Known partial fills remain observable without
changing positions directly; position truth continues to come from
`ACCOUNT_UPDATE` plus REST calibration.

An order outside the managed pair is rejected. An order whose client order ID
cannot be matched to the supplied local audit IDs enters HALT as
`UNKNOWN_ORDER`. Negative or internally impossible fill quantities fail
closed. This is observation and audit only; no cancel, replace, or submit
method is exposed.

## Offline core acceptance

- P1/P2 focused regression: 416 passed plus 3 subtests.
- Core upstream regression: 4381 passed, 20 skipped, 395 formally isolated,
  and no failures.
- Static analysis and full source compilation: passed.
- One-way baseline backtest: unchanged at 31 trades, -3.76% total profit,
  64.5% win rate, and 7.28% maximum drawdown.

The 395 isolated tests use the same boundary documented for P1: optional
FreqAI/Torch coverage, the live external vulnerability audit, and the one
workspace-configuration-polluted timeframe test that passes in a clean working
directory.
