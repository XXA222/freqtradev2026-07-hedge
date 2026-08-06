# R3.5 Binance USD-M Testnet execution

R3.5 isolates Binance USD-M Testnet credentials, endpoints and account identity from
mainnet.  The installer is offline and sends no order.  The optional canary command
supports `/fapi/v1/order/test` validation and an explicitly confirmed passive LIMIT
submit/cancel cycle on Testnet only.
