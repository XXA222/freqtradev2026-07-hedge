"""Freqtrade Hedge extension public surface.

The package intentionally avoids importing exchange-specific dependencies at module import
time.  This keeps planning, simulation, persistence, risk and execution usable in isolation,
and lets the main application enable only the runtime components it actually needs.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    # Legacy P1/P2 facade.
    "BinanceAccountRisk": ("freqtrade.hedge.binance_readonly", "BinanceAccountRisk"),
    "BinanceAccountSnapshot": ("freqtrade.hedge.binance_readonly", "BinanceAccountSnapshot"),
    "BinanceReadonlyAdapter": ("freqtrade.hedge.binance_readonly", "BinanceReadonlyAdapter"),
    "BinanceReadonlyCheck": ("freqtrade.hedge.binance_readonly", "BinanceReadonlyCheck"),
    "BinanceReadonlyIssue": ("freqtrade.hedge.binance_readonly", "BinanceReadonlyIssue"),
    "BinanceReadonlyState": ("freqtrade.hedge.binance_readonly", "BinanceReadonlyState"),
    "BinanceRiskAssessment": ("freqtrade.hedge.binance_readonly", "BinanceRiskAssessment"),
    "parse_binance_account_risk": (
        "freqtrade.hedge.binance_readonly",
        "parse_binance_account_risk",
    ),
    "parse_binance_position": ("freqtrade.hedge.binance_readonly", "parse_binance_position"),
    "BinanceBalanceUpdate": ("freqtrade.hedge.binance_user_stream", "BinanceBalanceUpdate"),
    "BinanceOrderUpdate": ("freqtrade.hedge.binance_user_stream", "BinanceOrderUpdate"),
    "BinanceStreamState": ("freqtrade.hedge.binance_user_stream", "BinanceStreamState"),
    "BinanceStreamStatus": ("freqtrade.hedge.binance_user_stream", "BinanceStreamStatus"),
    "BinanceUserStreamMirror": (
        "freqtrade.hedge.binance_user_stream",
        "BinanceUserStreamMirror",
    ),
    "BinanceUserStreamUpdate": (
        "freqtrade.hedge.binance_user_stream",
        "BinanceUserStreamUpdate",
    ),
    "parse_binance_account_update": (
        "freqtrade.hedge.binance_user_stream",
        "parse_binance_account_update",
    ),
    "parse_binance_order_update": (
        "freqtrade.hedge.binance_user_stream",
        "parse_binance_order_update",
    ),
    # Shared domain/config facade.
    "HedgeRuntimeConfig": ("freqtrade.hedge.config", "HedgeRuntimeConfig"),
    "normalize_hedge_config": ("freqtrade.hedge.config", "normalize_hedge_config"),
    "validate_hedge_config": ("freqtrade.hedge.config", "validate_hedge_config"),
    "HedgeAction": ("freqtrade.hedge.domain", "HedgeAction"),
    "HedgeActionPlan": ("freqtrade.hedge.domain", "HedgeActionPlan"),
    "PositionKey": ("freqtrade.hedge.domain", "PositionKey"),
    "PositionRecord": ("freqtrade.hedge.position_book", "PositionRecord"),
    "SideAwarePositionBook": ("freqtrade.hedge.position_book", "SideAwarePositionBook"),
    "ReconciliationIssue": ("freqtrade.hedge.reconciliation", "ReconciliationIssue"),
    "ReconciliationResult": ("freqtrade.hedge.reconciliation", "ReconciliationResult"),
    "reconcile_positions": ("freqtrade.hedge.reconciliation", "reconcile_positions"),
    # Frozen P2-H2 public facade retained as lazy exports.
    "HedgeConfigurationError": ("freqtrade.hedge.errors", "HedgeConfigurationError"),
    "HedgeDataError": ("freqtrade.hedge.errors", "HedgeDataError"),
    "HedgeError": ("freqtrade.hedge.errors", "HedgeError"),
    "HedgeInvariantError": ("freqtrade.hedge.errors", "HedgeInvariantError"),
    "HedgeSafetyError": ("freqtrade.hedge.errors", "HedgeSafetyError"),
    "ReduceOnlyDecision": ("freqtrade.hedge.local_reduce_only", "ReduceOnlyDecision"),
    "calculate_safe_reduce": ("freqtrade.hedge.local_reduce_only", "calculate_safe_reduce"),
    "canonicalize_symbol": ("freqtrade.hedge.symbols", "canonicalize_symbol"),
    "raw_symbol": ("freqtrade.hedge.symbols", "raw_symbol"),
    "symbols_equivalent": ("freqtrade.hedge.symbols", "symbols_equivalent"),
    "AccountRiskSnapshot": ("freqtrade.hedge.risk", "AccountRiskSnapshot"),
    "HedgeRiskEngine": ("freqtrade.hedge.risk", "HedgeRiskEngine"),
    "RiskDecision": ("freqtrade.hedge.risk", "RiskDecision"),
    "RiskLimits": ("freqtrade.hedge.risk", "RiskLimits"),
    "PendingOrder": ("freqtrade.hedge.target_position", "PendingOrder"),
    "TargetDelta": ("freqtrade.hedge.target_position", "TargetDelta"),
    "TargetPosition": ("freqtrade.hedge.target_position", "TargetPosition"),
    "calculate_target_delta": ("freqtrade.hedge.target_position", "calculate_target_delta"),
    "target_idempotency_key": ("freqtrade.hedge.target_position", "target_idempotency_key"),
    "SimpleDualLegMaConfig": (
        "freqtrade.hedge.strategies.simple_ma_hedge",
        "SimpleDualLegMaConfig",
    ),
    "SimpleDualLegMaHedgeStrategy": (
        "freqtrade.hedge.strategies.simple_ma_hedge",
        "SimpleDualLegMaHedgeStrategy",
    ),
    "BinanceUserStreamLifecycle": (
        "freqtrade.hedge.user_stream_lifecycle",
        "BinanceUserStreamLifecycle",
    ),
    "UserStreamState": ("freqtrade.hedge.user_stream_lifecycle", "UserStreamState"),
    "UserStreamStatus": ("freqtrade.hedge.user_stream_lifecycle", "UserStreamStatus"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
