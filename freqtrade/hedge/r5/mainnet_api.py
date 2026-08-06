from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlencode

from freqtrade.hedge.execution.binance_usdm_adapter import HttpTransport, UrlLibHttpTransport
from freqtrade.hedge.exchange.shared_rate_limit import SqliteSharedWeightBudget

from .credentials import R5MainnetCredentials
from .market import R5Book, R5SymbolRules
from .models import R5AccountSnapshot, R5_ALLOWED_SYMBOLS


_FAPI = "https://fapi.binance.com"
_SAPI = "https://api.binance.com"
_ALLOWED = {
    ("GET", "/fapi/v1/time", False, _FAPI),
    ("GET", "/fapi/v1/exchangeInfo", False, _FAPI),
    ("GET", "/fapi/v1/ticker/bookTicker", False, _FAPI),
    ("GET", "/fapi/v1/positionSide/dual", True, _FAPI),
    ("GET", "/fapi/v1/multiAssetsMargin", True, _FAPI),
    ("GET", "/fapi/v1/symbolConfig", True, _FAPI),
    ("GET", "/fapi/v3/account", True, _FAPI),
    ("GET", "/fapi/v3/positionRisk", True, _FAPI),
    ("GET", "/fapi/v1/openOrders", True, _FAPI),
    ("GET", "/fapi/v1/income", True, _FAPI),
    ("GET", "/sapi/v1/account/apiRestrictions", True, _SAPI),
}


@dataclass(frozen=True, slots=True)
class R5LivePreflight:
    readonly_status: str
    hedge_mode_enabled: bool
    single_asset_mode: bool
    clock_offset_ms: int
    futures_trading_permission: bool
    ip_restricted: bool
    withdrawals_disabled: bool
    cross_margin_symbols: tuple[str, ...]
    account: R5AccountSnapshot
    rules: Mapping[str, R5SymbolRules]
    books: Mapping[str, R5Book]
    request_count: int
    account_can_trade: bool = True
    api_reading_permission: bool = True
    account_alias: str = ""
    evidence_account_fingerprint: str = ""
    evidence_account_fingerprint_candidates: tuple[str, ...] = ()
    evidence_account_fingerprint_modes: tuple[str, ...] = ()
    account_alias_present: bool = False
    managed_position_legs: tuple[Mapping[str, str], ...] = ()
    active_order_summaries: tuple[Mapping[str, str], ...] = ()
    balance_assets: tuple[str, ...] = ()
    wallet_balance: Decimal = Decimal("0")
    available_balance: Decimal = Decimal("0")
    leverage_by_symbol: Mapping[str, Decimal] = field(default_factory=dict)


class R5MainnetReadApi:
    """GET-only mainnet preflight client with exact-host allowlists and no redirects."""

    def __init__(
        self, *, credentials: R5MainnetCredentials, proxy_url: str | None = None,
        transport: HttpTransport | None = None, timeout_seconds: float = 10.0,
        now_ms=None, sleep=time.sleep,
        shared_weight_budget: SqliteSharedWeightBudget | None = None,
    ) -> None:
        self.credentials = credentials
        self.transport = transport or UrlLibHttpTransport(proxy_url=proxy_url)
        self.timeout = float(timeout_seconds)
        self.now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.sleep = sleep
        self.clock_offset_ms = 0
        self.request_count = 0
        self.shared_weight_budget = shared_weight_budget

    def preflight(self) -> R5LivePreflight:
        self.synchronize_clock()
        dual = self.signed_get(_FAPI, "/fapi/v1/positionSide/dual")
        multi = self.signed_get(_FAPI, "/fapi/v1/multiAssetsMargin")
        permissions = self.signed_get(_SAPI, "/sapi/v1/account/apiRestrictions")
        account = self.signed_get(_FAPI, "/fapi/v3/account")
        positions = self.signed_get(_FAPI, "/fapi/v3/positionRisk")
        open_orders = self.signed_get(_FAPI, "/fapi/v1/openOrders")
        exchange = self.public_get(_FAPI, "/fapi/v1/exchangeInfo")
        if not isinstance(dual, Mapping) or dual.get("dualSidePosition") is not True:
            raise PermissionError("R5 mainnet requires Hedge Mode")
        if not isinstance(multi, Mapping) or multi.get("multiAssetsMargin") is not False:
            raise PermissionError("R5 mainnet requires single-asset USD-M margin mode")
        if not isinstance(permissions, Mapping):
            raise RuntimeError("invalid Binance API restriction response")
        cross = []
        leverage_by_symbol: dict[str, Decimal] = {}
        for symbol in R5_ALLOWED_SYMBOLS:
            config = self.signed_get(_FAPI, "/fapi/v1/symbolConfig", {"symbol": symbol})
            rows = config if isinstance(config, list) else []
            matched = next((row for row in rows if isinstance(row, Mapping) and str(row.get("symbol", "")) == symbol), None)
            if matched is not None and str(matched.get("marginType", "")).upper() == "CROSSED":
                cross.append(symbol)
            if matched is not None:
                leverage_by_symbol[symbol] = Decimal(str(matched.get("leverage", "1")))
        if tuple(cross) != R5_ALLOWED_SYMBOLS:
            raise PermissionError("R5 requires Cross margin for BTCUSDT and ETHUSDT")
        snapshot = _account_snapshot(self.credentials.account_id, account, positions, open_orders)
        rules = _rules(exchange)
        books = {symbol: _book(symbol, self.public_get(_FAPI, "/fapi/v1/ticker/bookTicker", {"symbol": symbol}), rules[symbol]) for symbol in R5_ALLOWED_SYMBOLS}
        position_legs = _position_summaries(positions)
        order_summaries = _order_summaries(open_orders)
        balance_assets = _balance_assets(account)
        account_alias = str(account.get("accountAlias", "")).strip() if isinstance(account, Mapping) else ""
        fingerprint_candidates = self.credentials.r24_account_fingerprint_candidates(account_alias)
        primary_mode, primary_fingerprint = fingerprint_candidates[0]
        return R5LivePreflight(
            readonly_status="FULL_PASS", hedge_mode_enabled=True, single_asset_mode=True,
            clock_offset_ms=self.clock_offset_ms,
            futures_trading_permission=permissions.get("enableFutures") is True,
            ip_restricted=permissions.get("ipRestrict") is True,
            withdrawals_disabled=permissions.get("enableWithdrawals") is False,
            cross_margin_symbols=tuple(cross), account=snapshot, rules=rules, books=books, request_count=self.request_count,
            account_can_trade=account.get("canTrade") is True if isinstance(account, Mapping) else False,
            api_reading_permission=permissions.get("enableReading") is True,
            account_alias=account_alias,
            evidence_account_fingerprint=primary_fingerprint,
            evidence_account_fingerprint_candidates=tuple(
                fingerprint for _, fingerprint in fingerprint_candidates
            ),
            evidence_account_fingerprint_modes=tuple(
                mode for mode, _ in fingerprint_candidates
            ),
            account_alias_present=bool(account_alias),
            managed_position_legs=position_legs, active_order_summaries=order_summaries, balance_assets=balance_assets,
            wallet_balance=Decimal(str(account.get("totalWalletBalance", "0"))),
            available_balance=Decimal(str(account.get("availableBalance", "0"))),
            leverage_by_symbol=leverage_by_symbol,
        )

    def account_snapshot(self) -> R5AccountSnapshot:
        account = self.signed_get(_FAPI, "/fapi/v3/account")
        positions = self.signed_get(_FAPI, "/fapi/v3/positionRisk")
        open_orders = self.signed_get(_FAPI, "/fapi/v1/openOrders")
        return _account_snapshot(self.credentials.account_id, account, positions, open_orders)

    def income_history(
        self,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> tuple[Mapping[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("income history limit must be in [1, 1000]")
        payload = self.signed_get(
            _FAPI,
            "/fapi/v1/income",
            {"startTime": start_time_ms, "endTime": end_time_ms, "limit": limit},
        )
        if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
            raise RuntimeError("invalid Binance income response")
        return tuple(payload)

    def book(self, symbol: str) -> R5Book:
        exchange = self.public_get(_FAPI, "/fapi/v1/exchangeInfo")
        rules = _rules(exchange)[symbol]
        return _book(symbol, self.public_get(_FAPI, "/fapi/v1/ticker/bookTicker", {"symbol": symbol}), rules)

    def rules(self, symbol: str) -> R5SymbolRules:
        return _rules(self.public_get(_FAPI, "/fapi/v1/exchangeInfo"))[symbol]

    def synchronize_clock(self) -> int:
        before = self.now_ms()
        value = self.public_get(_FAPI, "/fapi/v1/time")
        after = self.now_ms()
        if not isinstance(value, Mapping) or not isinstance(value.get("serverTime"), int):
            raise RuntimeError("invalid Binance server time")
        self.clock_offset_ms = int(value["serverTime"]) - ((before + after) // 2)
        if abs(self.clock_offset_ms) > 1000:
            raise PermissionError("R5 clock offset exceeds 1000 ms")
        return self.clock_offset_ms

    def public_get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._perform(base, path, params or {}, authenticated=False)

    def signed_get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._perform(base, path, params or {}, authenticated=True)

    @staticmethod
    def _endpoint_weight(path: str) -> int:
        # Conservative local coordination weights. Remote Binance headers remain the
        # upper-bound authority and are folded back into the shared SQLite budget.
        return {
            "/fapi/v1/time": 1,
            "/fapi/v1/exchangeInfo": 10,
            "/fapi/v1/ticker/bookTicker": 2,
            "/fapi/v1/positionSide/dual": 30,
            "/fapi/v1/multiAssetsMargin": 30,
            "/fapi/v1/symbolConfig": 10,
            "/fapi/v3/account": 20,
            "/fapi/v3/positionRisk": 10,
            "/fapi/v1/openOrders": 10,
            "/fapi/v1/income": 30,
            "/sapi/v1/account/apiRestrictions": 5,
        }.get(path, 30)

    def _reserve_shared_weight(self, path: str) -> None:
        if self.shared_weight_budget is None:
            return
        weight = self._endpoint_weight(path)
        while True:
            decision = self.shared_weight_budget.reserve_weight(weight)
            if decision.granted:
                return
            self.sleep(decision.retry_after_seconds)

    def _observe_shared_weight(self, headers: Mapping[str, str]) -> None:
        if self.shared_weight_budget is None:
            return
        values: list[int] = []
        for name, raw in headers.items():
            if name.lower().startswith("x-mbx-used-weight"):
                try:
                    values.append(int(raw))
                except (TypeError, ValueError):
                    continue
        if values:
            self.shared_weight_budget.observe_remote_weight(max(values))

    def _perform(self, base: str, path: str, params: Mapping[str, Any], *, authenticated: bool) -> Any:
        if ("GET", path, authenticated, base) not in _ALLOWED:
            raise PermissionError("R5 readonly endpoint is not allowlisted")
        headers = {
            "Accept": "application/json",
            "User-Agent": "freqtrade-hedge-r5-mainnet/5.1.6",
        }
        if authenticated:
            headers["X-MBX-APIKEY"] = self.credentials.api_key
        last: Exception | None = None
        for attempt in range(3):
            self._reserve_shared_weight(path)
            values = {key: value for key, value in params.items() if value is not None}
            if authenticated:
                values["recvWindow"] = 5000
                values["timestamp"] = self.now_ms() + self.clock_offset_ms
                unsigned = urlencode(values)
                values["signature"] = hmac.new(
                    self.credentials.api_secret.encode(),
                    unsigned.encode(),
                    hashlib.sha256,
                ).hexdigest()
            query = urlencode(values)
            url = f"{base}{path}" + (f"?{query}" if query else "")
            self.request_count += 1
            try:
                response = self.transport.request("GET", url, headers, self.timeout)
            except TimeoutError as exc:
                last = exc
                if attempt == 2:
                    raise RuntimeError("R5 readonly transport failed") from exc
                self.sleep(0.25 * (2**attempt))
                continue
            self._observe_shared_weight(response.headers)
            try:
                payload = json.loads(response.payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Binance returned invalid JSON") from exc
            if response.status in {418, 429} or response.status >= 500:
                last = RuntimeError(f"temporary Binance HTTP {response.status}")
                if attempt == 2:
                    raise last
                retry = float(response.headers.get("Retry-After", "0") or 0)
                self.sleep(min(5.0, max(retry, 0.25 * (2**attempt))))
                continue
            if response.status >= 400:
                code = payload.get("code") if isinstance(payload, Mapping) else None
                message = payload.get("msg") if isinstance(payload, Mapping) else "request failed"
                raise PermissionError(
                    f"Binance readonly request rejected: HTTP {response.status}, code={code}, {message}"
                )
            return payload
        raise RuntimeError(f"unreachable R5 readonly request: {last}")


def _rules(exchange: Any) -> dict[str, R5SymbolRules]:
    if not isinstance(exchange, Mapping) or not isinstance(exchange.get("symbols"), list):
        raise RuntimeError("invalid exchangeInfo response")
    result = {}
    for row in exchange["symbols"]:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol", ""))
        if symbol not in R5_ALLOWED_SYMBOLS:
            continue
        if row.get("contractType") != "PERPETUAL" or row.get("status") != "TRADING":
            raise PermissionError(f"R5 symbol {symbol} is not a trading perpetual")
        filters = {str(item.get("filterType")): item for item in row.get("filters", []) if isinstance(item, Mapping)}
        result[symbol] = R5SymbolRules(
            symbol=symbol, tick_size=Decimal(str(filters["PRICE_FILTER"]["tickSize"])),
            qty_step=Decimal(str(filters["LOT_SIZE"]["stepSize"])),
            min_qty=Decimal(str(filters["LOT_SIZE"]["minQty"])), max_qty=Decimal(str(filters["LOT_SIZE"]["maxQty"])),
            min_notional=Decimal(str(filters["MIN_NOTIONAL"]["notional"])),
        )
    if set(result) != set(R5_ALLOWED_SYMBOLS):
        raise RuntimeError("exchangeInfo did not contain the complete R5 allowlist")
    return result


def _book(symbol: str, value: Any, rules: R5SymbolRules) -> R5Book:
    if not isinstance(value, Mapping):
        raise RuntimeError("invalid bookTicker response")
    bid = Decimal(str(value.get("bidPrice", "0")))
    ask = Decimal(str(value.get("askPrice", "0")))
    return R5Book(symbol, bid, ask, (bid + ask) / Decimal("2"))


def _account_snapshot(account_id: str, account: Any, positions: Any, open_orders: Any) -> R5AccountSnapshot:
    if not isinstance(account, Mapping) or not isinstance(positions, list) or not isinstance(open_orders, list):
        raise RuntimeError("invalid Binance account snapshot")
    long_notional = Decimal("0")
    short_notional = Decimal("0")
    unmanaged = set()
    for row in positions:
        if not isinstance(row, Mapping):
            continue
        qty = abs(Decimal(str(row.get("positionAmt", "0"))))
        if qty == 0:
            continue
        symbol = str(row.get("symbol", ""))
        mark = Decimal(str(row.get("markPrice", "0")))
        notional = abs(Decimal(str(row.get("notional", "0")))) if row.get("notional") not in (None, "") else qty * mark
        if symbol not in R5_ALLOWED_SYMBOLS:
            unmanaged.add(symbol)
            continue
        side = str(row.get("positionSide", "")).upper()
        if side == "LONG":
            long_notional += notional
        elif side == "SHORT":
            short_notional += notional
        else:
            unmanaged.add(f"{symbol}:{side or 'BOTH'}")
    for row in open_orders:
        if isinstance(row, Mapping) and str(row.get("symbol", "")) not in R5_ALLOWED_SYMBOLS:
            unmanaged.add(f"ORDER:{row.get('symbol', '')}")
    equity = Decimal(str(account.get("totalMarginBalance", account.get("totalWalletBalance", "0"))))
    return R5AccountSnapshot(account_id, equity, long_notional, short_notional, len(open_orders), tuple(unmanaged), datetime.now(UTC))


def _position_summaries(positions: Any) -> tuple[Mapping[str, str], ...]:
    result: list[Mapping[str, str]] = []
    if not isinstance(positions, list):
        return ()
    for row in positions:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol", ""))
        side = str(row.get("positionSide", "")).upper()
        if symbol not in R5_ALLOWED_SYMBOLS or side not in {"LONG", "SHORT"}:
            continue
        result.append({
            "symbol": symbol,
            "position_side": side,
            "position_amount": str(row.get("positionAmt", "0")),
            "entry_price": str(row.get("entryPrice", "0")),
            "mark_price": str(row.get("markPrice", "0")),
            "notional": str(row.get("notional", "0")),
            "unrealized_profit": str(row.get("unRealizedProfit", row.get("unrealizedProfit", "0"))),
            "margin_type": str(row.get("marginType", "")).upper(),
            "liquidation_price": str(row.get("liquidationPrice", "0")),
            "initial_margin": str(row.get("positionInitialMargin", row.get("initialMargin", "0"))),
            "maintenance_margin": str(row.get("maintMargin", row.get("maintenanceMargin", "0"))),
            "leverage": str(row.get("leverage", "1")),
        })
    return tuple(sorted(result, key=lambda item: (item["symbol"], item["position_side"])))


def _order_summaries(open_orders: Any) -> tuple[Mapping[str, str], ...]:
    result: list[Mapping[str, str]] = []
    if not isinstance(open_orders, list):
        return ()
    for row in open_orders:
        if not isinstance(row, Mapping):
            continue
        result.append({
            "symbol": str(row.get("symbol", "")),
            "side": str(row.get("side", "")),
            "position_side": str(row.get("positionSide", "")),
            "type": str(row.get("type", "")),
            "time_in_force": str(row.get("timeInForce", "")),
            "status": str(row.get("status", "")),
            "price": str(row.get("price", "0")),
            "original_quantity": str(row.get("origQty", "0")),
            "executed_quantity": str(row.get("executedQty", "0")),
            "reduce_only": str(bool(row.get("reduceOnly", False))).lower(),
            "order_id": str(row.get("orderId", "")),
            "client_order_id": str(row.get("clientOrderId", "")),
            "update_time": str(row.get("updateTime", row.get("time", ""))),
        })
    return tuple(result)


def _balance_assets(account: Any) -> tuple[str, ...]:
    if not isinstance(account, Mapping) or not isinstance(account.get("assets"), list):
        return ()
    assets = []
    for row in account["assets"]:
        if not isinstance(row, Mapping):
            continue
        wallet = Decimal(str(row.get("walletBalance", "0")))
        margin = Decimal(str(row.get("marginBalance", "0")))
        if wallet != 0 or margin != 0:
            assets.append(str(row.get("asset", "")))
    return tuple(sorted({asset for asset in assets if asset}))
