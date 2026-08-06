from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from .binance_client import BinanceUSDMReadonlyClient
from .common import assert_no_nonfinite, decimal_value, sha256_json
from .pagination import PageResult, fetch_all_orders, fetch_income, fetch_user_trades
from .validation import (
    Check,
    failed,
    passed,
    required_failures,
    validate_account_config_consistency,
    validate_account_equations,
    validate_balances,
    validate_clock,
    validate_cross_symbol_config,
    validate_exchange_precision,
    validate_modes,
    validate_orders_and_trades,
    validate_permissions,
    validate_positions,
    validate_trading_status,
    warned,
)


@dataclass(frozen=True)
class CollectionOptions:
    target_symbol: str = "BTCUSDT"
    history_days: int = 30
    page_limit: int = 1000
    clock_samples: int = 5
    max_symbols: int = 20
    strict_key_policy: bool = False
    require_single_asset_mode: bool = True
    require_active_dual_leg: bool = False

    def validate(self) -> None:
        if not self.target_symbol.isalnum() or self.target_symbol.upper() != self.target_symbol:
            raise ValueError("target_symbol must be a Binance symbol such as BTCUSDT")
        if not 1 <= self.history_days <= 90:
            raise ValueError("history_days must be between 1 and 90")
        if not 100 <= self.page_limit <= 1000:
            raise ValueError("page_limit must be between 100 and 1000")
        if not 1 <= self.max_symbols <= 100:
            raise ValueError("max_symbols must be between 1 and 100")


def pair_to_symbol(pair: str) -> str:
    base = pair.split(":", 1)[0]
    return "".join(character for character in base if character.isalnum()).upper()


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} returned {type(value).__name__}, expected object")
    return value


def _require_list(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{name} returned an invalid list")
    return value


def _current_anchor(client: BinanceUSDMReadonlyClient, target_symbol: str) -> dict[str, Any]:
    account = _require_dict(client.signed_get("/fapi/v3/account"), "account")
    balances = _require_list(client.signed_get("/fapi/v3/balance"), "balance")
    positions = _require_list(client.signed_get("/fapi/v3/positionRisk"), "positionRisk")
    target_positions = _require_list(
        client.signed_get("/fapi/v3/positionRisk", {"symbol": target_symbol}),
        "target positionRisk",
    )
    target_sides = {str(item.get("positionSide")) for item in target_positions}
    position_api_fallback = None
    if not {"LONG", "SHORT"}.issubset(target_sides):
        target_positions = _require_list(
            client.signed_get("/fapi/v2/positionRisk", {"symbol": target_symbol}),
            "target positionRisk V2 fallback",
        )
        position_api_fallback = "V2_TARGET_IDENTITY"
    position_map = {
        (str(item.get("symbol")), str(item.get("positionSide"))): item for item in positions
    }
    for item in target_positions:
        position_map[(str(item.get("symbol")), str(item.get("positionSide")))] = item
    open_orders = _require_list(client.signed_get("/fapi/v1/openOrders"), "openOrders")
    dual_mode = _require_dict(client.signed_get("/fapi/v1/positionSide/dual"), "position mode")
    multi_assets = _require_dict(
        client.signed_get("/fapi/v1/multiAssetsMargin"), "multi-assets mode"
    )
    result = {
        "account": account,
        "balances": balances,
        "positions": list(position_map.values()),
        "open_orders": open_orders,
        "dual_mode": dual_mode,
        "multi_assets": multi_assets,
        "position_api_fallback": position_api_fallback,
    }
    result["anchor_hash"] = sha256_json(result)
    return result


def _active_symbols(anchor: Mapping[str, Any], target_symbol: str, max_symbols: int) -> list[str]:
    symbols = {target_symbol}
    for position in anchor["positions"]:
        try:
            if decimal_value(
                position.get("positionAmt", "0"), field="active.positionAmt"
            ) != 0:
                symbols.add(str(position.get("symbol", "")))
        except (AttributeError, ValueError):
            pass
    symbols.update(str(order.get("symbol", "")) for order in anchor["open_orders"])
    symbols.discard("")
    ordered = [target_symbol, *sorted(symbol for symbol in symbols if symbol != target_symbol)]
    if len(ordered) > max_symbols:
        raise ValueError(
            f"Active/requested symbol scope ({len(ordered)}) exceeds max_symbols={max_symbols}"
        )
    return ordered


def _collect_histories(
    client: BinanceUSDMReadonlyClient,
    symbols: list[str],
    *,
    start_ms: int,
    end_ms: int,
    page_limit: int,
    collect_income: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    watermarks: dict[str, Any] = {}
    for symbol in symbols:
        order_page = fetch_all_orders(
            client.signed_get,
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=page_limit,
        )
        trade_page = fetch_user_trades(
            client.signed_get,
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=page_limit,
        )
        orders.extend(order_page.rows)
        trades.extend(trade_page.rows)
        watermarks[f"orders:{symbol}"] = order_page.watermark()
        watermarks[f"trades:{symbol}"] = trade_page.watermark()

    income_rows: list[dict[str, Any]] = []
    if collect_income:
        income_page: PageResult = fetch_income(
            client.signed_get,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=page_limit,
        )
        watermarks["income:ACCOUNT"] = income_page.watermark()
        income_rows = list(income_page.rows)
    return {
        "orders": orders,
        "trades": trades,
        "income": income_rows,
    }, watermarks


def _precision_catalog(exchange_info: Mapping[str, Any], symbols: list[str]) -> list[dict[str, Any]]:
    catalog = []
    raw_symbols = exchange_info.get("symbols", [])
    if not isinstance(raw_symbols, list):
        raise ValueError("exchangeInfo.symbols is not a list")
    for item in raw_symbols:
        if not isinstance(item, dict) or item.get("symbol") not in symbols:
            continue
        filters = {}
        for entry in item.get("filters", []):
            if isinstance(entry, dict) and entry.get("filterType"):
                filters[str(entry["filterType"])] = entry
        catalog.append(
            {
                "symbol": item.get("symbol"),
                "status": item.get("status"),
                "contractType": item.get("contractType"),
                "baseAsset": item.get("baseAsset"),
                "quoteAsset": item.get("quoteAsset"),
                "pricePrecision": item.get("pricePrecision"),
                "quantityPrecision": item.get("quantityPrecision"),
                "filters": filters,
            }
        )
    missing = sorted(set(symbols) - {str(item.get("symbol")) for item in catalog})
    if missing:
        raise ValueError(f"Symbols missing from USD-M exchangeInfo: {missing}")
    return catalog


def _account_position_reconciliation(account: Mapping[str, Any], positions: list[dict[str, Any]]) -> Check:
    def is_active(row: Mapping[str, Any]) -> bool:
        try:
            return decimal_value(row.get("positionAmt", "0"), field="positionAmt") != 0
        except ValueError:
            return True

    account_map = {
        (str(row.get("symbol")), str(row.get("positionSide"))): decimal_value(
            row.get("positionAmt", "0"), field="account.positionAmt"
        )
        for row in account.get("positions", [])
        if isinstance(row, dict) and is_active(row)
    }
    risk_map = {
        (str(row.get("symbol")), str(row.get("positionSide"))): decimal_value(
            row.get("positionAmt", "0"), field="risk.positionAmt"
        )
        for row in positions
        if is_active(row)
    }
    if account_map == risk_map:
        return passed(
            "reconcile.account_vs_position_risk",
            "Account and positionRisk active legs agree",
            active_leg_count=len(risk_map),
        )
    return failed(
        "reconcile.account_vs_position_risk",
        "Account and positionRisk active legs disagree",
        account_keys=sorted(f"{key[0]}:{key[1]}" for key in account_map),
        position_risk_keys=sorted(f"{key[0]}:{key[1]}" for key in risk_map),
    )


def collect_authenticated_snapshot(
    client: BinanceUSDMReadonlyClient,
    options: CollectionOptions,
) -> tuple[dict[str, Any], list[Check]]:
    options.validate()
    run_id = f"r24-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:12]}"
    clock = client.sync_clock(options.clock_samples)
    clock_summary = clock.to_summary()
    snapshot_start_ms = clock.samples[-1].server_time_ms

    permissions = _require_dict(
        client.signed_get("/sapi/v1/account/apiRestrictions"), "API restrictions"
    )
    trading_status = _require_dict(
        client.signed_get("/fapi/v1/apiTradingStatus"), "API trading status"
    )
    exchange_info = _require_dict(client.public_get("/fapi/v1/exchangeInfo"), "exchangeInfo")
    initial = _current_anchor(client, options.target_symbol)
    symbols = _active_symbols(initial, options.target_symbol, options.max_symbols)

    history_start_ms = snapshot_start_ms - options.history_days * 24 * 60 * 60 * 1000
    histories, watermarks = _collect_histories(
        client,
        symbols,
        start_ms=history_start_ms,
        end_ms=snapshot_start_ms,
        page_limit=options.page_limit,
    )

    optional_errors: list[dict[str, Any]] = []
    account_config = _require_dict(
        client.signed_get("/fapi/v1/accountConfig"), "accountConfig"
    )
    symbol_config: list[dict[str, Any]] = []
    for symbol in symbols:
        value = _require_list(
            client.signed_get("/fapi/v1/symbolConfig", {"symbol": symbol}),
            f"symbolConfig:{symbol}",
        )
        symbol_config.extend(value)
    all_leverage_brackets = _require_list(
        client.signed_get("/fapi/v1/leverageBracket"), "leverageBracket"
    )
    order_rate_limits = _require_list(
        client.signed_get("/fapi/v1/rateLimit/order"), "rateLimit/order"
    )
    commission_rates = []
    for symbol in symbols:
        value = _require_dict(
            client.signed_get("/fapi/v1/commissionRate", {"symbol": symbol}),
            f"commissionRate:{symbol}",
        )
        commission_rates.append(value)

    final = _current_anchor(client, options.target_symbol)
    scope_expanded_symbols: list[str] = []
    observed_final_symbols = _active_symbols(
        final, options.target_symbol, options.max_symbols
    )
    scope_expanded_symbols = [symbol for symbol in observed_final_symbols if symbol not in symbols]
    if scope_expanded_symbols:
        expansion_histories, expansion_watermarks = _collect_histories(
            client,
            scope_expanded_symbols,
            start_ms=history_start_ms,
            end_ms=snapshot_start_ms,
            page_limit=options.page_limit,
            collect_income=False,
        )
        for name in ("orders", "trades", "income"):
            if name == "income":
                continue  # account-wide income was already complete
            histories[name].extend(expansion_histories[name])
        watermarks.update(
            {
                name: value
                for name, value in expansion_watermarks.items()
                if not name.startswith("income:")
            }
        )
        for symbol in scope_expanded_symbols:
            symbol_config.extend(
                _require_list(
                    client.signed_get("/fapi/v1/symbolConfig", {"symbol": symbol}),
                    f"symbolConfig:{symbol}",
                )
            )
            commission_rates.append(
                _require_dict(
                    client.signed_get("/fapi/v1/commissionRate", {"symbol": symbol}),
                    f"commissionRate:{symbol}",
                )
            )
        symbols.extend(scope_expanded_symbols)
        final = _current_anchor(client, options.target_symbol)
    final_observed_symbols = _active_symbols(final, options.target_symbol, options.max_symbols)
    unresolved_new_symbols = [symbol for symbol in final_observed_symbols if symbol not in symbols]
    scoped_symbol_set = set(symbols)
    leverage_brackets = [
        row for row in all_leverage_brackets if str(row.get("symbol", "")) in scoped_symbol_set
    ]
    end_time_payload = _require_dict(client.public_get("/fapi/v1/time"), "final server time")
    snapshot_end_ms = int(end_time_payload["serverTime"])
    anchors_stable = initial["anchor_hash"] == final["anchor_hash"]

    account_alias = str(final["account"].get("accountAlias", "unknown"))
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": "2.4.1",
        "run_id": run_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "mode": "BINANCE_USDM_AUTHENTICATED_GET_ONLY",
        "account_fingerprint": client.account_fingerprint(account_alias),
        "scope": {
            "target_symbol": options.target_symbol,
            "symbols": symbols,
            "history_days": options.history_days,
            "history_start_ms": history_start_ms,
            "history_end_ms": snapshot_start_ms,
            "auto_expanded_symbols": scope_expanded_symbols,
            "unresolved_new_symbols": unresolved_new_symbols,
        },
        "clock": clock_summary,
        "snapshot_interval": {
            "start_server_ms": snapshot_start_ms,
            "end_server_ms": snapshot_end_ms,
            "duration_ms": snapshot_end_ms - snapshot_start_ms,
            "initial_anchor_hash": initial["anchor_hash"],
            "final_anchor_hash": final["anchor_hash"],
            "anchors_stable": anchors_stable,
        },
        "permissions": permissions,
        "key_policy": {
            "profile": "STRICT_PRODUCTION" if options.strict_key_policy else "READONLY_AUDIT",
            "strict": options.strict_key_policy,
        },
        "trading_status": trading_status,
        "modes": {
            "dual_side_position": final["dual_mode"],
            "multi_assets_margin": final["multi_assets"],
            "account_config": account_config,
            "position_api_fallback": final["position_api_fallback"],
        },
        "account": final["account"],
        "balances": final["balances"],
        "positions": final["positions"],
        "open_orders": final["open_orders"],
        **histories,
        "commission_rates": commission_rates,
        "leverage_brackets": leverage_brackets,
        "symbol_config": symbol_config,
        "order_rate_limits": order_rate_limits,
        "exchange_symbols": _precision_catalog(exchange_info, symbols),
        "watermarks": watermarks,
        "optional_endpoint_errors": optional_errors,
        "transport": {
            "request_count": client.request_count,
            "retry_count": client.retry_count,
            "weight_headers": dict(client.weight_headers),
        },
    }
    assert_no_nonfinite(snapshot)
    checks: list[Check] = []
    checks.extend(validate_clock(clock_summary))
    checks.extend(
        validate_permissions(
            permissions,
            futures_private_read_observed=True,
            strict_key_policy=options.strict_key_policy,
        )
    )
    checks.extend(
        validate_modes(
            final["dual_mode"],
            final["multi_assets"],
            require_single_asset_mode=options.require_single_asset_mode,
        )
    )
    checks.extend(
        validate_account_config_consistency(
            account_config,
            final["dual_mode"],
            final["multi_assets"],
        )
    )
    checks.extend(validate_cross_symbol_config(symbol_config, symbols))
    checks.extend(
        validate_positions(
            final["positions"],
            target_symbol=options.target_symbol,
            require_active_dual_leg=options.require_active_dual_leg,
        )
    )
    checks.extend(validate_account_equations(final["account"]))
    checks.extend(validate_balances(final["balances"]))
    checks.extend(
        validate_orders_and_trades(
            final["open_orders"], histories["orders"], histories["trades"]
        )
    )
    checks.extend(
        validate_exchange_precision(
            snapshot["exchange_symbols"],
            final["positions"],
            final["open_orders"],
            histories["trades"],
        )
    )
    checks.extend(validate_trading_status(trading_status))
    checks.append(_account_position_reconciliation(final["account"], final["positions"]))
    bracket_symbols = {str(row.get("symbol", "")) for row in leverage_brackets}
    missing_brackets = sorted(set(symbols) - bracket_symbols)
    checks.append(
        passed(
            "leverage.scope_complete",
            "Every scoped symbol has leverage-bracket data",
            symbol_count=len(bracket_symbols),
        )
        if not missing_brackets
        else failed(
            "leverage.scope_complete",
            "Leverage-bracket data is missing for scoped symbols",
            symbols=missing_brackets,
        )
    )
    if anchors_stable:
        checks.append(passed("snapshot.anchor_stability", "Current-account anchors remained stable"))
    else:
        checks.append(
            warned(
                "snapshot.anchor_stability",
                "Account changed during collection; interval and both anchor hashes were recorded",
            )
        )
    if unresolved_new_symbols:
        checks.append(
            failed(
                "history.scope_stability",
                "New active symbols appeared after adaptive scope expansion",
                symbols=unresolved_new_symbols,
            )
        )
    elif scope_expanded_symbols:
        checks.append(
            passed(
                "history.scope_stability",
                "History scope automatically expanded for symbols activated during collection",
                severity="ADVISORY",
                symbols=scope_expanded_symbols,
            )
        )
    else:
        checks.append(passed("history.scope_stability", "No new active symbols appeared during collection"))
    if final["position_api_fallback"]:
        checks.append(
            passed(
                "positions.api_version",
                "Position V3 returned active/open-order rows and V2 supplied zero-position target identities",
                severity="ADVISORY",
                fallback=final["position_api_fallback"],
            )
        )
    else:
        checks.append(
            passed(
                "positions.api_version",
                "Position V3 exposed both target side identities",
                severity="ADVISORY",
            )
        )
    incomplete = [name for name, item in watermarks.items() if item.get("complete") is not True]
    checks.append(
        passed("history.pagination", "Every history stream reached a terminal page", stream_count=len(watermarks))
        if not incomplete
        else failed("history.pagination", "Incomplete history streams", streams=incomplete)
    )
    snapshot["validation"] = {
        "required_failures": required_failures(checks),
        "checks": [check.to_dict() for check in checks],
    }
    snapshot["snapshot_hash"] = sha256_json(snapshot)
    return snapshot, checks
