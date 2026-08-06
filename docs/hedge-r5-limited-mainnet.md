# Freqtrade Hedge R5 Limited Mainnet Production

R5 introduces seven mandatory promotion stages:

1. MAINNET_READONLY
2. ORDER_TEST
3. SUBMIT_CANCEL
4. SINGLE_LEG
5. DUAL_LEG
6. RESTRICTED_AUTO
7. EXPANDED_LIMITS

A stage cannot run before durable evidence for the previous stage exists. Completed runs are idempotent. An interrupted active write stage becomes RECOVERY_REQUIRED and is never blindly resubmitted.

## Safety boundaries

- Binance USD-M perpetual only.
- BTCUSDT and ETHUSDT only.
- Hedge Mode and Cross margin required.
- MARKET orders disabled.
- API key must have Futures permission, IP restriction, and withdrawals disabled.
- Operator and independent risk tokens are required for position-creating stages.
- Expanded limits require a signed promotion manifest.
- Default maximum single order is 50 USDT even at EXPANDED_LIMITS. The code hard cap is 250 USDT.
- Installer and diagnostics are offline and send zero exchange requests.

## Credential file

The dedicated mainnet credential file contains exactly three lines:

1. Binance API key
2. Binance API secret
3. BINANCE_USDM_MAINNET_R5

Do not reuse a file without the environment marker.

## Online execution

Use scripts/Run-Freqtrade-Hedge-R5-Mainnet-Stage.ps1 only after the combined R4/R5 offline installation and diagnostics pass. Secret values are read from one-line files and are not passed directly on the command line.

RESTRICTED_AUTO and EXPANDED_LIMITS require an explicit strategy bridge in module:callable form. No default automatic strategy is silently enabled.
