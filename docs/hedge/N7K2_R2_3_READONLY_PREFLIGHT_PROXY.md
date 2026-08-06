# N7K2 R2.3 — Binance read-only preflight and explicit proxy support

## Scope

R2.3 adds the first operational gate required before the 24-hour and 72-hour Binance read-only soak. It does not enable order submission. Native and Hedge exchange writes remain locked.

## New command

```powershell
freqtrade hedge-readonly-check `
  --config user_data\config_hedge_readonly.json `
  --output user_data\audit\readonly-preflight.json
```

The command performs only REST reads:

- server-time synchronization;
- API restriction inspection;
- full account, balances and positions;
- all open orders;
- Hedge Mode, Cross margin and leverage confirmation;
- unmanaged position/order detection.

`--include-history` additionally reads order, fill and income history. It still does not create a listen key or open a WebSocket.

## Proxy behavior

The canonical Hedge REST and user-stream clients now support explicit HTTP(S) proxy URLs. Resolution order is:

1. `hedge.rest_proxy_url` / `hedge.websocket_proxy_url`;
2. `exchange.ccxt_async_config.httpsProxy`;
3. `exchange.ccxt_config.httpsProxy`;
4. environment proxy only when `hedge.trust_env_proxy=true`.

Proxy URLs are validated and never emitted in the preflight report. Embedded proxy credentials are rejected.

## Fail-closed gates

The report returns `HALT` and the command exits non-zero when any of the following is observed:

- API read/futures permission policy failure;
- withdrawal or transfer permission enabled when forbidden by policy;
- account not in Hedge Mode;
- managed symbols not uniformly Cross margin;
- configured target leverage mismatch;
- any nonzero unmanaged position;
- any active unmanaged order;
- clock skew above the configured maximum.

## Safety boundary

- Runtime started: NO
- Listen key created: NO
- WebSocket connected: NO
- Order endpoint called: NO
- Exchange writes: LOCKED

A passing preflight is necessary but not sufficient for live trading. The next external evidence gate remains a 24-hour then 72-hour read-only soak.
