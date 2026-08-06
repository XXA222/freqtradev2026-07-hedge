
# P1A: Hedge domain and safe configuration gate

This stage introduces the domain vocabulary and configuration contract only.
It does not enable dual-side order execution.

## Added configuration

```json
{
  "position_mode": "oneway",
  "hedge_mode_enabled": false,
  "managed_pair": "ETH/USDT:USDT",
  "hedge": {
    "exchange_adapter": "binance",
    "read_only": true
  }
}
```

Safety rules:

- Existing configurations default to `oneway`.
- Hedge mode requires futures and cross margin.
- Enabling the hedge feature requires exactly one managed pair.
- P1 intentionally rejects `hedge.read_only=false`.
- Execution remains locked until persistence, reconciliation, exchange mapping,
  and risk controls are implemented and tested.

## Domain identity

Position identity is now represented as:

```text
(exchange, symbol, position_side)
```

Position sides are `BOTH`, `LONG`, and `SHORT`. Hedge actions may target only
`LONG` or `SHORT`.
