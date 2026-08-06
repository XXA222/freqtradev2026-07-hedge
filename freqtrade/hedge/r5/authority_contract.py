"""Frozen authority boundaries for Hedge production execution."""

HEDGE_FACT_AUTHORITY = "SQL_HEDGE_LEDGER"
NATIVE_TRADE_ROLE = "READ_ONLY_COMPATIBILITY_PROJECTION"
R5_LEGACY_WRITE_ROLE = "DISABLED"
AUTHORITATIVE_EXECUTION_ENTRYPOINT = "freqtrade.hedge.r5.authoritative_cli"
AUTHORITATIVE_CONTROL_ENTRYPOINT = "freqtrade.hedge.r5.control_adapter.R5AuthoritativeControlAdapter"
SUPPORTED_HEDGE_EXECUTION_EXCHANGES = ("binance",)
