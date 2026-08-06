# R3.5 Binance USD-M Testnet Execution

R3.5 adds a guarded Binance USD-M Testnet execution path without enabling mainnet.

## Boundaries

- REST: `https://demo-fapi.binance.com`
- WebSocket: `wss://demo-fstream.binance.com/ws`
- Account namespace: `binance-usdm-testnet:<fingerprint>`
- Symbols: BTCUSDT and ETHUSDT perpetual only
- Maximum order notional: 25 USDT
- Market orders: disabled
- Mainnet credentials: never loaded by R3.5

## Credential file

Use a dedicated UTF-8 file with exactly three logical lines:

1. Testnet API key
2. Testnet API secret
3. `BINANCE_USDM_TESTNET`

The marker prevents accidental reuse of the existing mainnet two-line credential file.

## Canary modes

- `validation-only`: calls `/fapi/v1/order/test`; no order is placed.
- `submit-cancel`: submits one explicitly supplied passive LIMIT order and immediately
  cancels it. This mode requires a second command-line confirmation.

Both modes require a process-local arming token supplied through the
`FREQTRADE_HEDGE_TESTNET_ARM_TOKEN` environment variable. Write requests are never
retried automatically. Ambiguous outcomes enter UNKNOWN/query recovery.
