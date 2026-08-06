# R3.6 Unified Testnet E2E

R3.6 is a cumulative development layer over R3.4 and R3.5. It provides a single resumable
orchestrator for Binance USD-M Testnet and leaves Binance mainnet permanently locked.

## Workflow

1. Load the dedicated Testnet credential file with the `BINANCE_USDM_TESTNET` marker.
2. Start the Testnet read-only runtime and require `READY` health.
3. Read public Testnet exchange rules and mark price.
4. Construct a passive `GTX` limit order for BTCUSDT or ETHUSDT.
5. Validate the order with `/fapi/v1/order/test`.
6. In explicitly confirmed submit/cancel mode, submit once, query, cancel and verify terminal state.
7. Persist every state transition in SQLite WAL.
8. On restart, query a previously submitted order and never blindly resubmit it.

## Safety boundaries

- Mainnet endpoints and account namespaces are rejected.
- Only BTCUSDT and ETHUSDT perpetual contracts are accepted.
- Maximum Testnet order notional is 25 USDT.
- Market orders are disabled.
- Submit/cancel requires a process-local arming token and explicit confirmation.
- The offline installer never loads credentials, accesses Binance or sends orders.


## R3.6.2 Windows SQLite cleanup

`TestnetRunJournal` now owns a closing context manager for every SQLite connection, supports explicit `close()`, checkpoints WAL on close, and the acceptance workflow closes both the original and reopened journal before deleting temporary files.
