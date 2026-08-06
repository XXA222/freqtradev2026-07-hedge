"""Complete upstream + Hedge Dry-run configuration gates for Docker runtime."""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


_ALLOWED = frozenset(
    {
        "enabled",
        "required_python",
        "network_probe_attempts",
        "network_min_success_ratio",
        "network_max_last_candle_age_seconds",
        "network_max_missing_candles",
        "max_strategy_cycle_age_seconds",
        "max_reconciliation_age_seconds",
        "permission_probe_path",
        "evidence_path",
        "require_proxy_trust_env",
        "position_adjustment_owner",
        "api_exposure_mode",
    }
)

_REQUIRED_TOP_LEVEL = (
    "dry_run",
    "trading_mode",
    "margin_mode",
    "max_open_trades",
    "stake_currency",
    "stake_amount",
    "timeframe",
    "strategy",
    "exchange",
    "pairlists",
    "entry_pricing",
    "exit_pricing",
    "order_types",
    "order_time_in_force",
    "unfilledtimeout",
    "cancel_open_orders_on_exit",
    "telegram",
    "api_server",
    "hedge",
)


@dataclass(frozen=True, slots=True)
class ConfigGateReport:
    ready: bool
    checks: dict[str, bool]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def validate_r561_config(config: Mapping[str, Any]) -> tuple[str, ...]:
    hedge = config.get("hedge", {})
    hedge = hedge if isinstance(hedge, Mapping) else {}
    raw = hedge.get("r561", {})
    raw = raw if isinstance(raw, Mapping) else {}
    if not raw:
        return ()
    errors: list[str] = []
    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        errors.append("R561_UNKNOWN_KEYS:" + ",".join(unknown))
    if not isinstance(raw.get("enabled", False), bool):
        errors.append("R561_ENABLED_MUST_BE_BOOL")
    required_python = str(raw.get("required_python", "3.12"))
    if required_python != "3.12":
        errors.append("R561_REQUIRED_PYTHON_MUST_BE_3_12")
    for name, minimum in (
        ("network_probe_attempts", 1),
        ("network_max_last_candle_age_seconds", 1),
        ("network_max_missing_candles", 0),
        ("max_strategy_cycle_age_seconds", 1),
        ("max_reconciliation_age_seconds", 1),
    ):
        try:
            value = int(raw.get(name, minimum))
            if value < minimum:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"R561_{name.upper()}_INVALID")
    try:
        ratio = _decimal(raw.get("network_min_success_ratio", "1"), "ratio")
        if ratio <= 0 or ratio > 1:
            raise ValueError
    except ValueError:
        errors.append("R561_NETWORK_MIN_SUCCESS_RATIO_INVALID")
    for key in ("permission_probe_path", "evidence_path"):
        path = str(raw.get(key, "user_data/hedge/r561"))
        if not path.strip() or ".." in Path(path).parts:
            errors.append(f"R561_{key.upper()}_INVALID")
    owner = str(raw.get("position_adjustment_owner", "hedge_planner"))
    if owner != "hedge_planner":
        errors.append("R561_POSITION_ADJUSTMENT_OWNER_INVALID")
    exposure_mode = str(raw.get("api_exposure_mode", "local_process"))
    if exposure_mode not in {"local_process", "docker_loopback_publish"}:
        errors.append("R561_API_EXPOSURE_MODE_INVALID")
    return tuple(dict.fromkeys(errors))


def validate_docker_dryrun_config(
    config: MutableMapping[str, Any],
    *,
    run_upstream_schema: bool = False,
) -> ConfigGateReport:
    checks: dict[str, bool] = {}
    errors: list[str] = list(validate_r561_config(config))
    warnings: list[str] = []
    checks["standard_required_fields"] = all(key in config for key in _REQUIRED_TOP_LEVEL)
    if not checks["standard_required_fields"]:
        missing = [key for key in _REQUIRED_TOP_LEVEL if key not in config]
        errors.append("STANDARD_REQUIRED_FIELDS_MISSING:" + ",".join(missing))

    hedge = config.get("hedge", {})
    hedge = hedge if isinstance(hedge, Mapping) else {}
    exchange = config.get("exchange", {})
    exchange = exchange if isinstance(exchange, Mapping) else {}
    telegram = config.get("telegram", {})
    telegram = telegram if isinstance(telegram, Mapping) else {}
    api = config.get("api_server", {})
    api = api if isinstance(api, Mapping) else {}

    checks["dry_run"] = config.get("dry_run") is True
    checks["read_only"] = hedge.get("read_only") is True
    checks["live_trading_locked"] = hedge.get("live_trading_enabled") is False
    checks["empty_exchange_credentials"] = not str(exchange.get("key", "")) and not str(
        exchange.get("secret", "")
    )
    checks["force_entry_disabled"] = config.get("force_entry_enable", False) is False
    checks["position_adjustment_owned_by_hedge"] = (
        config.get("position_adjustment_enable", False) is False
        and str(hedge.get("r561", {}).get("position_adjustment_owner", "hedge_planner"))
        == "hedge_planner"
    )
    checks["telegram_schema_complete"] = all(key in telegram for key in ("enabled", "token", "chat_id"))
    api_listen = str(api.get("listen_ip_address", "127.0.0.1"))
    exposure_mode = str(hedge.get("r561", {}).get("api_exposure_mode", "local_process"))
    if exposure_mode == "docker_loopback_publish":
        # The service must listen on the container interface; the Compose source audit
        # independently requires the published host port to bind to 127.0.0.1.
        checks["api_loopback"] = api_listen in {"0.0.0.0", "::"}
    else:
        checks["api_loopback"] = api_listen in {"127.0.0.1", "::1", "localhost"}

    sync = exchange.get("ccxt_config", {})
    sync = sync if isinstance(sync, Mapping) else {}
    async_cfg = exchange.get("ccxt_async_config", {})
    async_cfg = async_cfg if isinstance(async_cfg, Mapping) else {}
    require_trust = bool(hedge.get("r561", {}).get("require_proxy_trust_env", True))
    checks["ccxt_sync_trust_env"] = not require_trust or sync.get("requests_trust_env") is True
    checks["ccxt_async_trust_env"] = not require_trust or async_cfg.get("aiohttp_trust_env") is True

    risk_keys = (
        "max_gross_notional",
        "max_gross_exposure_ratio",
        "max_single_order_notional",
        "max_margin_utilization",
        "min_liquidation_buffer_ratio",
        "target_leverage",
    )
    checks["authoritative_risk_fields"] = all(key in hedge for key in risk_keys)
    checks["risk_limits_constructible"] = False
    try:
        from freqtrade.hedge.risk.limits import RiskLimits

        RiskLimits(
            max_gross_notional=_decimal(hedge.get("max_gross_notional"), "max_gross_notional"),
            max_gross_exposure_ratio=_decimal(
                hedge.get("max_gross_exposure_ratio"), "max_gross_exposure_ratio"
            ),
            max_single_order_notional=_decimal(
                hedge.get("max_single_order_notional"), "max_single_order_notional"
            ),
            max_margin_utilization=_decimal(
                hedge.get("max_margin_utilization"), "max_margin_utilization"
            ),
            min_liquidation_buffer_ratio=_decimal(
                hedge.get("min_liquidation_buffer_ratio"), "min_liquidation_buffer_ratio"
            ),
        )
        checks["risk_limits_constructible"] = True
    except Exception as exc:
        errors.append(f"RISK_LIMITS_CONSTRUCTION_FAILED:{type(exc).__name__}:{exc}")

    try:
        from freqtrade.hedge.config import normalize_hedge_config

        normalize_hedge_config(deepcopy(config))
        checks["hedge_normalization"] = True
    except Exception as exc:
        checks["hedge_normalization"] = False
        errors.append(f"HEDGE_NORMALIZATION_FAILED:{type(exc).__name__}:{exc}")

    if run_upstream_schema:
        try:
            from freqtrade.configuration.config_validation import validate_config_consistency
            from freqtrade.enums import RunMode

            copy = deepcopy(config)
            copy["runmode"] = RunMode.DRY_RUN
            validate_config_consistency(copy)
            checks["upstream_freqtrade_schema"] = True
        except Exception as exc:
            checks["upstream_freqtrade_schema"] = False
            errors.append(f"UPSTREAM_SCHEMA_FAILED:{type(exc).__name__}:{exc}")
    else:
        warnings.append("UPSTREAM_SCHEMA_NOT_EXECUTED")

    for name, passed in checks.items():
        if not passed:
            errors.append(f"CHECK_FAILED:{name}")
    return ConfigGateReport(
        ready=not errors,
        checks=checks,
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
    )
