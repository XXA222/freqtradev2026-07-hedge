# R3.6.2 Unified Binance USD-M Testnet E2E

R3.6 provides one durable entry point for Testnet read-only preflight, market-rule discovery,
`/order/test` validation and an optional passive GTX submit/cancel canary. The workflow uses a
SQLite WAL journal with compare-and-swap transitions, replays completed runs without network
access, and recovers interrupted submitted orders by querying before any cancellation. It never
accepts Binance mainnet endpoints or credentials.

The offline installer executes only fake transports. Online Testnet execution is a separate,
explicit runner and requires a dedicated three-line Testnet credential file plus a process-local
arming token.


R3.6.2 closes every short-lived SQLite connection deterministically and adds explicit journal context management for Windows cleanup.
