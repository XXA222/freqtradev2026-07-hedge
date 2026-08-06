# Freqtrade-Hedge R5.1.6 Mainnet Read-only Deep Contract Audit

R5.1.6 is the first live-observation stage of R5. It never enables an order endpoint.

## Mandatory read-only success contract

A MAINNET_READONLY run completes only when the exact Binance mainnet hosts are used, authenticated GET requests succeed, Hedge Mode and single-asset USD-M mode are active, BTCUSDT and ETHUSDT are Cross, clock skew is within 1000 ms, R2.4/R2.5/live account identity is bound, user-stream health is fresh, reconciliation and SQLite integrity pass, and exchange order writes remain zero.

The listenKey create/renew/close lifecycle is a control-plane operation. It does not submit, amend, cancel, test, or create an exchange order.

## Observation and promotion are separate

The following facts do not invalidate a successful read-only observation:

- Futures TRADE permission is disabled;
- IP restriction is disabled;
- account trading is disabled;
- an allowed BTCUSDT/ETHUSDT position exists;
- allowed-symbol activity orders exist.

They instead produce explicit promotion blocks. ORDER_TEST remains unavailable until all configured key-policy requirements pass and the account is flat with no active orders.

Possible next-stage values are:

- `ORDER_TEST`
- `BLOCKED_KEY_POLICY`
- `BLOCKED_ACCOUNT_STATE`
- `BLOCKED_KEY_AND_ACCOUNT_STATE`
- `BLOCKED_PROMOTION_POLICY`

## Refreshable evidence

MAINNET_READONLY may be run repeatedly. The latest successful observation becomes current promotion evidence, while every prior run remains immutable in `r5_stage_evidence_history`. Replaying an older completed run validates against its historical digest instead of the latest stage row.

ORDER_TEST requires the latest MAINNET_READONLY evidence to be both fresh and `order_test_promotion_ready=true`.

## Credential boundary

R5.1 reads the operator-selected two-line credential file directly. It does not create a usable credential copy under `user_data`. An exact stale file created by R5.1.0-R5.1.4 is removed only when its key, secret, and marker exactly match the selected source credentials. Any different file is left untouched.

The online runner temporarily exposes credentials to the existing R2.5 user-stream process through environment variables and restores the previous environment in `finally`. Diagnostic artifacts are secret-scanned by the R2.5 layer.

## Account identity binding

R5 reproduces the R2.4 HMAC account fingerprint with the current API key and secret. If `/fapi/v3/account` omits `accountAlias`, it uses the exact R2.4.1 legacy fallback value `unknown`; if the alias is present, both the live-alias and legacy-compatible candidates are evaluated. The accepted fingerprint must match the R2.4 report and R2.5 scope fingerprint.

## Retry and Windows process contract

Every signed GET retry regenerates `timestamp` and `signature`. PowerShell 5.1 captures stdout and stderr into separate temporary files, streams them to the console and durable log, deletes temporary files, and returns only an integer exit code.

## Current-account interpretation

If ETHUSDT currently has two managed legs and five active orders while the API key has Futures TRADE permission disabled, the correct result is:

- MAINNET_READONLY: complete;
- exchange order writes: zero;
- ORDER_TEST promotion: blocked by key policy and account state.

R5.1.6 never cancels the orders or closes the positions.
