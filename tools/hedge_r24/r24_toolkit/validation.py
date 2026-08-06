from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .common import decimal_value


REQUIRED = "REQUIRED"
ADVISORY = "ADVISORY"
EVIDENCE = "EVIDENCE"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    severity: str
    summary: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def passed(name: str, summary: str, *, severity: str = REQUIRED, **details: Any) -> Check:
    return Check(name, "PASS", severity, summary, details)


def failed(name: str, summary: str, *, severity: str = REQUIRED, **details: Any) -> Check:
    return Check(name, "FAIL", severity, summary, details)


def warned(name: str, summary: str, **details: Any) -> Check:
    return Check(name, "WARN", ADVISORY, summary, details)


def observed(name: str, summary: str, **details: Any) -> Check:
    return Check(name, "OBSERVED", EVIDENCE, summary, details)


def not_observed(name: str, summary: str, **details: Any) -> Check:
    return Check(name, "NOT_OBSERVED", EVIDENCE, summary, details)


def _bool_check(
    name: str,
    actual: Any,
    expected: bool,
    summary: str,
    *,
    severity: str = REQUIRED,
) -> Check:
    if actual is expected:
        return passed(name, summary, severity=severity, actual=actual, expected=expected)
    return failed(name, summary, severity=severity, actual=actual, expected=expected)


def validate_permissions(
    permissions: Mapping[str, Any],
    *,
    futures_private_read_observed: bool,
    strict_key_policy: bool,
) -> list[Check]:
    """Validate permissions without conflating USER_DATA reads with TRADE access.

    Binance reports ``enableFutures`` as the key's futures *trading* permission.
    A successful set of authenticated ``/fapi`` USER_DATA requests is stronger
    evidence for the read-only capability exercised by this toolkit.  R2.4.1
    therefore keeps trading permission and private-read evidence separate.
    """
    checks = [
        _bool_check("permission.reading", permissions.get("enableReading"), True, "API reading enabled"),
        _bool_check(
            "permission.futures_private_read",
            futures_private_read_observed,
            True,
            "Authenticated USD-M Futures USER_DATA reads succeeded",
        ),
        _bool_check(
            "permission.withdrawals_disabled",
            permissions.get("enableWithdrawals"),
            False,
            "Withdrawals disabled",
        ),
        _bool_check(
            "permission.internal_transfer_disabled",
            permissions.get("enableInternalTransfer", False),
            False,
            "Internal transfer disabled",
        ),
        _bool_check(
            "permission.universal_transfer_disabled",
            permissions.get("permitsUniversalTransfer", False),
            False,
            "Universal transfer disabled",
        ),
    ]
    futures_trading = permissions.get("enableFutures")
    if futures_trading is True:
        checks.append(
            passed(
                "permission.futures_trading",
                "USD-M Futures trading permission is enabled",
                severity=REQUIRED if strict_key_policy else ADVISORY,
                actual=True,
                strict_key_policy=strict_key_policy,
            )
        )
    elif strict_key_policy:
        checks.append(
            failed(
                "permission.futures_trading",
                "Strict key policy requires USD-M Futures trading permission",
                actual=futures_trading,
                expected=True,
                strict_key_policy=True,
            )
        )
    else:
        checks.append(
            warned(
                "permission.futures_trading",
                "USD-M Futures trading permission is disabled; authenticated readonly access remains usable",
                actual=futures_trading,
                strict_key_policy=False,
            )
        )
    ip_restricted = permissions.get("ipRestrict") is True
    if ip_restricted:
        checks.append(
            passed(
                "permission.ip_restricted",
                "API key is IP-restricted",
                severity=REQUIRED if strict_key_policy else ADVISORY,
                actual=True,
                strict_key_policy=strict_key_policy,
            )
        )
    elif strict_key_policy:
        checks.append(
            failed(
                "permission.ip_restricted",
                "Strict key policy requires an API IP whitelist",
                actual=permissions.get("ipRestrict"),
                expected=True,
                strict_key_policy=True,
            )
        )
    else:
        checks.append(
            warned(
                "permission.ip_restricted",
                "API key is not IP-restricted; acceptable for this readonly audit but not production-ready",
                actual=permissions.get("ipRestrict"),
                strict_key_policy=False,
            )
        )
    return checks


def validate_clock(clock: Mapping[str, Any], *, max_abs_offset_ms: int = 1000) -> list[Check]:
    offset = int(clock.get("offset_ms", 10**9))
    max_rtt = float(clock.get("max_rtt_ms", 10**9))
    spread = float(clock.get("offset_spread_ms", 10**9))
    checks = []
    if abs(offset) <= max_abs_offset_ms:
        checks.append(passed("clock.offset", "Clock offset within required bound", offset_ms=offset))
    else:
        checks.append(
            failed(
                "clock.offset",
                "Clock offset exceeds required bound",
                offset_ms=offset,
                maximum_ms=max_abs_offset_ms,
                local_clock_relation="AHEAD_OF_BINANCE" if offset < 0 else "BEHIND_BINANCE",
                remediation_code="WINDOWS_TIME_RESYNC_REQUIRED",
            )
        )
    if max_rtt <= 3000 and spread <= 2000:
        checks.append(
            passed("clock.quality", "Clock samples are usable", max_rtt_ms=max_rtt, spread_ms=spread)
        )
    else:
        checks.append(
            failed(
                "clock.quality",
                "Clock samples are unstable or too slow",
                max_rtt_ms=max_rtt,
                spread_ms=spread,
            )
        )
    return checks


def validate_modes(
    dual_mode: Mapping[str, Any],
    multi_assets: Mapping[str, Any],
    *,
    require_single_asset_mode: bool,
) -> list[Check]:
    checks = [
        _bool_check(
            "mode.hedge",
            dual_mode.get("dualSidePosition"),
            True,
            "Binance Hedge Mode enabled",
        )
    ]
    actual_multi = multi_assets.get("multiAssetsMargin")
    if require_single_asset_mode:
        checks.append(
            _bool_check(
                "mode.single_asset",
                actual_multi,
                False,
                "Single-asset USD-M margin mode enabled",
            )
        )
    else:
        checks.append(
            passed(
                "mode.multi_asset_recorded",
                "Multi-assets setting recorded",
                severity=ADVISORY,
                multiAssetsMargin=actual_multi,
            )
        )
    return checks


def validate_account_config_consistency(
    account_config: Mapping[str, Any],
    dual_mode: Mapping[str, Any],
    multi_assets: Mapping[str, Any],
) -> list[Check]:
    checks: list[Check] = []
    comparisons = (
        ("dualSidePosition", dual_mode.get("dualSidePosition")),
        ("multiAssetsMargin", multi_assets.get("multiAssetsMargin")),
    )
    for field, expected in comparisons:
        if field not in account_config:
            checks.append(
                warned(
                    f"account_config.{field}",
                    f"accountConfig did not include {field}; dedicated endpoint remains authoritative",
                )
            )
        elif account_config.get(field) == expected:
            checks.append(
                passed(
                    f"account_config.{field}",
                    f"accountConfig and dedicated {field} endpoint agree",
                )
            )
        else:
            checks.append(
                failed(
                    f"account_config.{field}",
                    f"accountConfig and dedicated {field} endpoint disagree",
                    account_config=account_config.get(field),
                    dedicated_endpoint=expected,
                )
            )
    return checks


def validate_cross_symbol_config(
    symbol_config: Iterable[Mapping[str, Any]], scoped_symbols: Iterable[str]
) -> list[Check]:
    config_map = {str(item.get("symbol", "")): item for item in symbol_config}
    missing = sorted(set(scoped_symbols) - set(config_map))
    isolated = []
    unknown = []
    for symbol in scoped_symbols:
        if symbol not in config_map:
            continue
        margin_type = str(config_map[symbol].get("marginType", "")).upper()
        if margin_type == "ISOLATED":
            isolated.append(symbol)
        elif margin_type not in {"CROSS", "CROSSED"}:
            unknown.append(f"{symbol}:{margin_type or 'MISSING'}")
    checks: list[Check] = []
    if missing:
        checks.append(failed("margin.symbol_config_complete", "Symbol margin configuration is missing", symbols=missing))
    else:
        checks.append(passed("margin.symbol_config_complete", "All scoped symbols have margin configuration"))
    if isolated or unknown:
        checks.append(
            failed(
                "margin.cross",
                "Every scoped symbol must be configured for Cross margin",
                isolated=isolated,
                unknown=unknown,
            )
        )
    else:
        checks.append(
            passed(
                "margin.cross",
                "Every scoped symbol is configured for Cross margin",
                symbol_count=len(set(scoped_symbols)),
            )
        )
    return checks


def _position_side(position: Mapping[str, Any]) -> str:
    return str(position.get("positionSide", "")).upper()


def validate_positions(
    positions: Iterable[Mapping[str, Any]],
    *,
    target_symbol: str,
    require_active_dual_leg: bool,
) -> list[Check]:
    rows = list(positions)
    keys = [(str(row.get("symbol", "")), _position_side(row)) for row in rows]
    checks: list[Check] = []
    if len(keys) == len(set(keys)):
        checks.append(passed("positions.unique_side_key", "Position keys are unique by symbol and side", row_count=len(rows)))
    else:
        checks.append(failed("positions.unique_side_key", "Duplicate symbol/positionSide keys detected"))

    active = []
    active_sides_by_symbol: dict[str, set[str]] = defaultdict(set)
    invalid_sides = []
    isolated = []
    direction_errors = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        side = _position_side(row)
        amount = decimal_value(row.get("positionAmt", "0"), field=f"{symbol}.{side}.positionAmt")
        if amount != 0:
            active.append(row)
            active_sides_by_symbol[symbol].add(side)
        if side not in {"LONG", "SHORT"}:
            invalid_sides.append(f"{symbol}:{side}")
        isolated_flag = row.get("isolated") is True or str(row.get("marginType", "")).upper() == "ISOLATED"
        if amount != 0 and isolated_flag:
            isolated.append(f"{symbol}:{side}")
        if side == "LONG" and amount < 0:
            direction_errors.append(f"{symbol}:LONG:{amount}")
        if side == "SHORT" and amount > 0:
            direction_errors.append(f"{symbol}:SHORT:{amount}")
    checks.append(
        passed("positions.hedge_sides_only", "All position rows use LONG/SHORT", row_count=len(rows))
        if not invalid_sides
        else failed("positions.hedge_sides_only", "BOTH/unknown side present in Hedge Mode", keys=invalid_sides)
    )
    checks.append(
        passed(
            "positions.no_isolated_marker",
            "No active position carries an explicit isolated marker",
            severity=ADVISORY,
            active_count=len(active),
        )
        if not isolated
        else failed("positions.no_isolated_marker", "Isolated active positions detected", keys=isolated)
    )
    checks.append(
        passed("positions.direction", "LONG/SHORT signed quantities are consistent")
        if not direction_errors
        else failed("positions.direction", "Position quantities contradict positionSide", rows=direction_errors)
    )

    target = {side: row for row in rows if row.get("symbol") == target_symbol for side in [_position_side(row)]}
    if {"LONG", "SHORT"}.issubset(target):
        checks.append(
            passed(
                "positions.target_side_identity",
                "Target symbol exposes independent LONG and SHORT records",
                target_symbol=target_symbol,
            )
        )
        long_amount = abs(decimal_value(target["LONG"].get("positionAmt", "0"), field="target.LONG"))
        short_amount = abs(decimal_value(target["SHORT"].get("positionAmt", "0"), field="target.SHORT"))
        if long_amount > 0 and short_amount > 0:
            checks.append(
                observed(
                    "positions.target_active_dual_leg",
                    "Target symbol has simultaneous active LONG and SHORT legs",
                    target_symbol=target_symbol,
                )
            )
        else:
            checks.append(
                not_observed(
                    "positions.target_active_dual_leg",
                    "Target symbol did not have simultaneous nonzero LONG and SHORT legs",
                    target_symbol=target_symbol,
                )
            )
    else:
        checks.append(
            failed(
                "positions.target_side_identity",
                "Target symbol did not expose separate LONG and SHORT records",
                target_symbol=target_symbol,
                sides=sorted(target),
            )
        )
    dual_leg_symbols = sorted(
        symbol
        for symbol, sides in active_sides_by_symbol.items()
        if {"LONG", "SHORT"}.issubset(sides)
    )
    if dual_leg_symbols:
        checks.append(
            observed(
                "positions.active_dual_leg",
                "At least one scoped symbol has simultaneous active LONG and SHORT legs",
                symbols=dual_leg_symbols,
                target_observed=target_symbol in dual_leg_symbols,
            )
        )
    elif require_active_dual_leg:
        checks.append(
            failed(
                "positions.active_dual_leg_required",
                "An active LONG and SHORT pair was required but not observed on any scoped symbol",
                scoped_symbols=sorted(active_sides_by_symbol),
            )
        )
    else:
        checks.append(
            not_observed(
                "positions.active_dual_leg",
                "No scoped symbol had simultaneous nonzero LONG and SHORT legs",
                scoped_symbols=sorted(active_sides_by_symbol),
            )
        )
    return checks


def validate_account_equations(account: Mapping[str, Any]) -> list[Check]:
    equations = (("account.margin_balance", "totalWalletBalance", "totalUnrealizedProfit", "totalMarginBalance"),)
    checks: list[Check] = []
    tolerance = Decimal("0.00000001")
    for name, left_field, right_field, total_field in equations:
        try:
            left = decimal_value(account.get(left_field), field=left_field)
            right = decimal_value(account.get(right_field), field=right_field)
            total = decimal_value(account.get(total_field), field=total_field)
            delta = abs((left + right) - total)
        except ValueError as exc:
            checks.append(failed(name, "Account equation fields are invalid", error=str(exc)))
            continue
        if delta <= tolerance:
            checks.append(passed(name, "Account balance equation reconciles", delta=str(delta)))
        else:
            checks.append(failed(name, "Account balance equation does not reconcile", delta=str(delta)))
    cross_fields = ("totalCrossWalletBalance", "totalCrossUnPnl")
    try:
        cross_balance = decimal_value(account.get(cross_fields[0]), field=cross_fields[0])
        cross_pnl = decimal_value(account.get(cross_fields[1]), field=cross_fields[1])
        checks.append(
            passed(
                "account.cross_margin_derived",
                "Cross margin balance was derived from V3 wallet and unrealized PnL",
                severity=ADVISORY,
                derived_total=str(cross_balance + cross_pnl),
            )
        )
    except ValueError as exc:
        checks.append(failed("account.cross_margin_derived", "Cross balance fields are invalid", error=str(exc)))
    return checks


def validate_orders_and_trades(
    open_orders: Iterable[Mapping[str, Any]],
    orders: Iterable[Mapping[str, Any]],
    trades: Iterable[Mapping[str, Any]],
) -> list[Check]:
    open_rows, order_rows, trade_rows = list(open_orders), list(orders), list(trades)
    checks: list[Check] = []
    side_errors = []
    for entity, rows in (("open_order", open_rows),):
        for row in rows:
            if str(row.get("positionSide", "")).upper() not in {"LONG", "SHORT"}:
                side_errors.append(f"{entity}:{row.get('symbol')}:{row.get('orderId')}:{row.get('positionSide')}")
    checks.append(
        passed("orders.position_side", "Orders and trades preserve LONG/SHORT identity")
        if not side_errors
        else failed("orders.position_side", "BOTH/unknown positionSide detected", rows=side_errors[:50])
    )
    historical_one_way = []
    for entity, rows in (("order", order_rows), ("trade", trade_rows)):
        for row in rows:
            if str(row.get("positionSide", "")).upper() not in {"LONG", "SHORT"}:
                historical_one_way.append(
                    f"{entity}:{row.get('symbol')}:{row.get('orderId')}:{row.get('positionSide')}"
                )
    if historical_one_way:
        checks.append(
            warned(
                "history.legacy_position_side",
                "Historical BOTH/unknown rows exist, possibly from an earlier one-way-mode period",
                count=len(historical_one_way),
                rows=historical_one_way[:50],
            )
        )
    else:
        checks.append(
            passed(
                "history.position_side",
                "Historical orders and trades preserve LONG/SHORT identity",
                severity=ADVISORY,
            )
        )

    valid_open_status = {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
    invalid_open = [
        f"{row.get('symbol')}:{row.get('orderId')}:{row.get('status')}"
        for row in open_rows
        if str(row.get("status", "")).upper() not in valid_open_status
    ]
    checks.append(
        passed("orders.open_status", "Open-order statuses are active", count=len(open_rows))
        if not invalid_open
        else failed("orders.open_status", "Terminal status found in open orders", rows=invalid_open)
    )

    order_map = {(str(row.get("symbol")), str(row.get("orderId"))): row for row in order_rows}
    trade_qty: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for row in trade_rows:
        trade_qty[(str(row.get("symbol")), str(row.get("orderId")))] += abs(
            decimal_value(row.get("qty", "0"), field="trade.qty")
        )
    quantity_errors = []
    for key, quantity in trade_qty.items():
        order = order_map.get(key)
        if order is None:
            continue
        executed = abs(decimal_value(order.get("executedQty", "0"), field="order.executedQty"))
        if quantity > executed + Decimal("0.00000001"):
            quantity_errors.append(f"{key[0]}:{key[1]} trades={quantity} executed={executed}")
    checks.append(
        passed("orders.fill_quantity", "Trade quantities do not exceed order executed quantities")
        if not quantity_errors
        else failed("orders.fill_quantity", "Trade/order quantity inconsistency", rows=quantity_errors[:50])
    )
    return checks


def validate_balances(balances: Iterable[Mapping[str, Any]]) -> list[Check]:
    rows = list(balances)
    assets = [str(row.get("asset", "")) for row in rows]
    checks: list[Check] = []
    if all(assets) and len(assets) == len(set(assets)):
        checks.append(passed("balances.unique_asset", "Balance assets are unique", asset_count=len(assets)))
    else:
        checks.append(failed("balances.unique_asset", "Missing or duplicate asset balance rows"))
    decimal_fields = (
        "balance",
        "crossWalletBalance",
        "crossUnPnl",
        "availableBalance",
        "maxWithdrawAmount",
    )
    errors = []
    for row in rows:
        for field in decimal_fields:
            if field not in row:
                continue
            try:
                decimal_value(row[field], field=f"{row.get('asset')}.{field}")
            except ValueError as exc:
                errors.append(str(exc))
    checks.append(
        passed("balances.decimal_finite", "All balance quantities are finite decimals")
        if not errors
        else failed("balances.decimal_finite", "Invalid balance decimal values", errors=errors[:50])
    )
    return checks


def _multiple(value: Any, increment: Any, *, field: str) -> bool:
    number = abs(decimal_value(value, field=field))
    step = abs(decimal_value(increment, field=f"{field}.increment"))
    if number == 0 or step == 0:
        return True
    return number % step == 0


def validate_exchange_precision(
    exchange_symbols: Iterable[Mapping[str, Any]],
    positions: Iterable[Mapping[str, Any]],
    open_orders: Iterable[Mapping[str, Any]],
    trades: Iterable[Mapping[str, Any]],
) -> list[Check]:
    catalog = {str(item.get("symbol")): item for item in exchange_symbols}
    errors: list[str] = []
    for item in [*positions, *open_orders, *trades]:
        symbol = str(item.get("symbol", ""))
        market = catalog.get(symbol)
        if market is None:
            errors.append(f"{symbol}:missing_exchange_metadata")
            continue
        filters = market.get("filters", {})
        lot = filters.get("LOT_SIZE", {}) if isinstance(filters, dict) else {}
        price_filter = filters.get("PRICE_FILTER", {}) if isinstance(filters, dict) else {}
        quantity_value = None
        for field in ("positionAmt", "origQty", "qty"):
            if item.get(field) is not None:
                quantity_value = item[field]
                break
        if quantity_value is not None and lot.get("stepSize") is not None:
            try:
                if not _multiple(quantity_value, lot["stepSize"], field=f"{symbol}.quantity"):
                    errors.append(f"{symbol}:quantity_not_step_aligned:{quantity_value}")
            except ValueError as exc:
                errors.append(str(exc))
        price_value = item.get("price")
        if price_value is not None and price_filter.get("tickSize") is not None:
            try:
                if not _multiple(price_value, price_filter["tickSize"], field=f"{symbol}.price"):
                    errors.append(f"{symbol}:price_not_tick_aligned:{price_value}")
            except ValueError as exc:
                errors.append(str(exc))
    if errors:
        return [failed("exchange.precision", "Quantity/price precision mismatch detected", errors=errors[:100])]
    return [passed("exchange.precision", "Positions, open orders, and trades align to exchange increments")]


def validate_trading_status(payload: Mapping[str, Any]) -> list[Check]:
    locked: list[str] = []
    indicators = payload.get("indicators", {})
    if isinstance(indicators, dict):
        for symbol, values in indicators.items():
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict) and item.get("isLocked") is True:
                        locked.append(str(symbol))
    if payload.get("isLocked") is True:
        locked.append("ACCOUNT")
    if locked:
        return [failed("account.api_trading_status", "Binance API trading lock detected", symbols=sorted(set(locked)))]
    return [passed("account.api_trading_status", "No Binance API trading lock reported")]


def required_failures(checks: Iterable[Check]) -> list[str]:
    return [check.name for check in checks if check.severity == REQUIRED and check.status == "FAIL"]
