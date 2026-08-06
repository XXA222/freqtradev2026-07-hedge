Freqtrade-Hedge R5.1.6 Mainnet Read-only Legacy Evidence Binding Audit

Version: 5.1.6
Delivery: 20260804-2132-FEAC

This cumulative release fixes the live Binance USD-M account identity binding.
Binance may omit accountAlias from /fapi/v3/account. R2.4.1 historically
substituted the literal value "unknown" before computing its HMAC fingerprint.
R5.1.6 reproduces that exact legacy contract and validates the current API key
and secret against both R2.4 and R2.5 evidence without requiring accountAlias.

MAINNET_READONLY remains GET-only except for listenKey lifecycle operations.
No order endpoint is enabled and no credentials are copied into the project.
