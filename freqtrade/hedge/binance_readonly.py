"""Full-account, read-only Binance USDⓈ-M inspection."""

from __future__ import annotations

import warnings

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

import ccxt

from freqtrade.enums import PositionMode, PositionSide
from freqtrade.exceptions import DDosProtection, OperationalException, TemporaryError
from freqtrade.hedge.config import HedgeRuntimeConfig
from freqtrade.hedge.errors import HedgeDataError
from freqtrade.hedge.numeric import require_nonnegative, to_decimal
from freqtrade.hedge.position_book import PositionRecord
from freqtrade.hedge.reconciliation import (
    ReconciliationResult,
    reconcile_positions,
    records_from_trades,
)
from freqtrade.hedge.symbols import canonicalize_symbol, symbols_equivalent


class BinanceReadonlyApi(Protocol):
    def fapiPrivateGetPositionSideDual(self) -> Mapping[str, Any]: ...
    def fetch_positions(
        self,
        symbols: list[str] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]: ...
    def fetch_balance(
        self,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...
    def fetch_open_orders(
        self,
        symbol: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...
    def fetch_my_trades(
        self,
        symbol: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...
    def fetch_time(self) -> int: ...


@dataclass(frozen=True, slots=True)
class BinanceAccountSnapshot:
    observed_at: datetime
    observed_at_ms: int
    server_time_ms: int
    clock_skew_ms: int
    position_mode: PositionMode
    positions: tuple[PositionRecord, ...]
    open_orders: tuple[Mapping[str, Any], ...]
    recent_fills: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class BinanceReadonlyIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class BinanceAccountRisk:
    wallet_balance: Decimal
    equity: Decimal
    available_balance: Decimal
    initial_margin: Decimal
    maintenance_margin: Decimal
    margin_ratio: Decimal


class BinanceReadonlyState(StrEnum):
    CONSISTENT = "CONSISTENT"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class BinanceReadonlyCheck:
    managed_pair: str
    configured_position_mode: PositionMode
    snapshot: BinanceAccountSnapshot
    reconciliation: ReconciliationResult
    issues: tuple[BinanceReadonlyIssue, ...]

    @property
    def consistent(self) -> bool:
        return not self.issues and self.reconciliation.consistent

    def to_dict(self) -> dict[str, Any]:
        serialized_issues = [
            {"code": issue.code, "detail": issue.detail} for issue in self.issues
        ]
        return {
            "exchange": "binance",
            "managed_pair": self.managed_pair,
            "consistent": self.consistent,
            "observed_at": self.snapshot.observed_at.isoformat(),
            "observed_at_ms": self.snapshot.observed_at_ms,
            "server_time_ms": self.snapshot.server_time_ms,
            "clock_skew_ms": self.snapshot.clock_skew_ms,
            "configured_position_mode": self.configured_position_mode.value,
            "account_position_mode": self.snapshot.position_mode.value,
            "positions": [
                {
                    "symbol": record.symbol,
                    "position_side": record.position_side.value,
                    "amount": str(record.amount),
                }
                for record in self.snapshot.positions
            ],
            "open_order_count": len(self.snapshot.open_orders),
            "recent_fill_count": len(self.snapshot.recent_fills),
            "reconciliation_issues": [
                {"code": issue.code, "detail": issue.detail} for issue in self.reconciliation.issues
            ],
            # Keep the original key and expose the newer semantic alias.
            "issues": serialized_issues,
            "safety_issues": serialized_issues,
        }


@dataclass(frozen=True, slots=True)
class BinanceRiskAssessment:
    account: BinanceAccountRisk
    check: BinanceReadonlyCheck
    state: BinanceReadonlyState
    halt_reasons: tuple[str, ...]

    @property
    def allows_risk_increase(self) -> bool:
        return self.state is BinanceReadonlyState.CONSISTENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "allows_risk_increase": self.allows_risk_increase,
            "account": {
                "wallet_balance": str(self.account.wallet_balance),
                "equity": str(self.account.equity),
                "available_balance": str(self.account.available_balance),
                "initial_margin": str(self.account.initial_margin),
                "maintenance_margin": str(self.account.maintenance_margin),
                "margin_ratio": str(self.account.margin_ratio),
            },
            "halt_reasons": list(self.halt_reasons),
            "check": self.check.to_dict(),
        }


def _value(source: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = source.get(name)
        if value is not None:
            return value
    info = source.get("info")
    if isinstance(info, Mapping):
        for name in names:
            value = info.get(name)
            if value is not None:
                return value
    return default


def _explicit_position_side(source: Mapping[str, Any]) -> PositionSide | None:
    raw = _value(source, "positionSide")
    if raw is None:
        return None
    try:
        return PositionSide(str(raw).upper())
    except ValueError as exc:
        raise OperationalException(f"Unsupported Binance positionSide: {raw!r}.") from exc


def _parse_position_amount(
    position: Mapping[str, Any],
    *,
    explicit_side: PositionSide | None,
) -> Decimal:
    info = position.get("info")
    info = info if isinstance(info, Mapping) else {}
    has_signed_amount = "positionAmt" in position or "positionAmt" in info
    field = "positionAmt" if has_signed_amount else "contracts"
    raw_amount = (
        _value(position, "positionAmt")
        if has_signed_amount
        else _value(position, "contracts", "amount", default=0)
    )
    try:
        amount = to_decimal(raw_amount, field=field)
    except HedgeDataError as exc:
        label = "positionAmt" if has_signed_amount else "amount"
        raise OperationalException(f"Binance position response contains invalid {label}.") from exc
    if amount is None:  # pragma: no cover - allow_none is deliberately false.
        raise OperationalException(f"Binance position response is missing {field}.")
    if has_signed_amount:
        if explicit_side is PositionSide.LONG and amount < 0:
            raise OperationalException("LONG positionAmt must not be negative.")
        if explicit_side is PositionSide.SHORT and amount > 0:
            raise OperationalException("SHORT positionAmt must not be positive.")
    elif amount < 0:
        raise OperationalException("Unified contracts must not be negative.")
    return amount


def parse_binance_position(
    position: Mapping[str, Any],
    *,
    managed_pair: str | None = None,
    account_id: str = "default",
    observed_at_ms: int | None = None,
    strict_hedge: bool = True,
) -> PositionRecord | None:
    explicit_side = _explicit_position_side(position)
    amount = _parse_position_amount(position, explicit_side=explicit_side)

    if amount == 0:
        return None

    unified_side_raw = str(_value(position, "side", default="")).lower()
    if explicit_side is PositionSide.BOTH:
        # Retain the historical, single-pair read-only adapter behavior while
        # keeping explicit P2 accounts fail-closed.
        if strict_hedge and managed_pair is not None:
            raise OperationalException("Nonzero BOTH position is invalid in Hedge Mode.")
        side = PositionSide.LONG if amount > 0 else PositionSide.SHORT
    elif explicit_side in {PositionSide.LONG, PositionSide.SHORT}:
        side = explicit_side
    elif unified_side_raw == "long":
        side = PositionSide.LONG
    elif unified_side_raw == "short":
        side = PositionSide.SHORT
    else:
        raise OperationalException("Binance position direction is ambiguous.")

    symbol_raw = _value(position, "symbol")
    if not isinstance(symbol_raw, str) or not symbol_raw.strip():
        raise OperationalException("Binance position response is missing symbol.")
    symbol = canonicalize_symbol(symbol_raw, managed_pair=managed_pair)

    margin_mode = str(_value(position, "marginMode", "marginType", default="unknown")).lower()
    return PositionRecord(
        exchange="binance",
        account_id=account_id,
        symbol=symbol,
        position_side=side,
        amount=abs(amount),
        entry_price=_value(position, "entryPrice", "entry_price", default=0),
        mark_price=_value(position, "markPrice", "mark_price"),
        liquidation_price=_value(
            position,
            "liquidationPrice",
            "liquidation_price",
        ),
        leverage=_value(position, "leverage", default=1),
        collateral=_value(
            position,
            "initialMargin",
            "collateral",
            "initial_margin",
            default=0,
        ),
        unrealized_pnl=_value(
            position,
            "unRealizedProfit",
            "unrealizedPnl",
            default=0,
        ),
        margin_mode=margin_mode,
        source="binance",
        source_time_ms=observed_at_ms,
    )


def _decimal_field(source: Mapping[str, Any], *names: str) -> Decimal:
    raw = _value(source, *names)
    if raw is None:
        raise OperationalException(f"Binance account response is missing {names[0]}.")
    try:
        return require_nonnegative(raw, field=names[0])
    except Exception as exc:
        raise OperationalException(
            f"Binance account response contains invalid {names[0]}."
        ) from exc


def parse_binance_account_risk(balance: Mapping[str, Any]) -> BinanceAccountRisk:
    wallet_balance = _decimal_field(balance, "totalWalletBalance", "walletBalance")
    equity = _decimal_field(balance, "totalMarginBalance", "equity")
    available = _decimal_field(balance, "availableBalance", "available_balance")
    initial = _decimal_field(balance, "totalInitialMargin", "initialMargin")
    maintenance = _decimal_field(
        balance,
        "totalMaintMargin",
        "maintenanceMargin",
    )
    if equity <= 0:
        raise OperationalException("Binance account equity must be positive.")
    return BinanceAccountRisk(
        wallet_balance=wallet_balance,
        equity=equity,
        available_balance=available,
        initial_margin=initial,
        maintenance_margin=maintenance,
        margin_ratio=maintenance / equity,
    )


class BinanceReadonlyAdapter:
    def __init__(
        self,
        api: BinanceReadonlyApi,
        config: HedgeRuntimeConfig,
    ) -> None:
        warnings.warn(
            "Legacy Hedge Binance compatibility stack is deprecated; "
            "use freqtrade.hedge.exchange and freqtrade.hedge.readonly.",
            DeprecationWarning,
            stacklevel=2,
        )
        if config.exchange_adapter != "binance":
            raise OperationalException("Binance adapter requires hedge.exchange_adapter='binance'.")
        if not config.read_only or config.live_trading_enabled:
            raise OperationalException("P2 Binance adapter is read-only.")
        self._api = api
        self._config = config

    def _read(self, operation: str, call: Any) -> Any:
        try:
            return call()
        except ccxt.DDoSProtection as exc:
            raise DDosProtection(f"Binance read-only {operation} was rate limited.") from exc
        except (ccxt.AuthenticationError, ccxt.PermissionDenied) as exc:
            raise OperationalException(f"Binance read-only {operation} was denied.") from exc
        except (ccxt.OperationFailed, ccxt.ExchangeError) as exc:
            raise TemporaryError(f"Binance read-only {operation} failed temporarily.") from exc
        except ccxt.BaseError as exc:
            raise OperationalException(f"Binance read-only {operation} failed.") from exc

    def fetch_position_mode(self) -> PositionMode:
        response = self._read(
            "position-mode read",
            self._api.fapiPrivateGetPositionSideDual,
        )
        if not isinstance(response, Mapping):
            raise OperationalException("Position-mode response must be an object.")
        dual = response.get("dualSidePosition")
        if not isinstance(dual, bool):
            raise OperationalException("Position-mode response requires boolean dualSidePosition.")
        return PositionMode.HEDGE if dual else PositionMode.ONEWAY

    def fetch_all_positions(
        self,
        *,
        observed_at_ms: int,
        managed_pair: str | None = None,
    ) -> tuple[PositionRecord, ...]:
        raw_positions = self._read(
            "full-account position read",
            lambda: self._api.fetch_positions(
                [managed_pair] if managed_pair is not None else None,
                params={},
            ),
        )
        if not isinstance(raw_positions, list) or not all(
            isinstance(item, Mapping) for item in raw_positions
        ):
            raise OperationalException("Positions response must be a list of objects.")
        records = (
            parse_binance_position(
                item,
                managed_pair=self._config.managed_pair,
                account_id=self._config.account_id,
                observed_at_ms=observed_at_ms,
                strict_hedge=self._config.account_id != "default",
            )
            for item in raw_positions
        )
        return tuple(
            sorted(
                (record for record in records if record is not None),
                key=lambda record: record.key,
            )
        )

    def fetch_positions(self) -> tuple[PositionRecord, ...]:
        """Compatibility wrapper for the original single-pair read API."""
        return self.fetch_all_positions(
            observed_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            managed_pair=self._config.managed_pair,
        )

    def fetch_account_risk(self) -> BinanceAccountRisk:
        balance = self._read(
            "account-risk read",
            lambda: self._api.fetch_balance(params={}),
        )
        if not isinstance(balance, Mapping):
            raise OperationalException("Account response must be an object.")
        return parse_binance_account_risk(balance)

    def fetch_snapshot(self, managed_pair: str | None = None) -> BinanceAccountSnapshot:
        if managed_pair is not None and self._config.managed_pair not in {None, managed_pair}:
            raise OperationalException("managed_pair does not match the configured Hedge pair.")
        observed = datetime.now(UTC)
        observed_ms = int(observed.timestamp() * 1000)
        server_ms = self._read("server-time read", self._api.fetch_time)
        if isinstance(server_ms, bool) or not isinstance(server_ms, int):
            if managed_pair is None:
                raise OperationalException("Binance server time must be integer milliseconds.")
            server_ms = observed_ms
        positions = self.fetch_all_positions(
            observed_at_ms=server_ms,
            managed_pair=managed_pair,
        )
        open_orders = self._read(
            "full-account open-order read",
            lambda: self._api.fetch_open_orders(None, params={}),
        )
        recent_fills = self._read(
            "full-account trade read",
            lambda: self._api.fetch_my_trades(None, params={}),
        )
        if not isinstance(open_orders, Sequence) or isinstance(open_orders, (str, bytes)):
            if managed_pair is None:
                raise OperationalException("Open-order response must be a sequence.")
            open_orders = ()
        if not isinstance(recent_fills, Sequence) or isinstance(recent_fills, (str, bytes)):
            if managed_pair is None:
                raise OperationalException("Trade response must be a sequence.")
            recent_fills = ()
        return BinanceAccountSnapshot(
            observed_at=observed,
            observed_at_ms=observed_ms,
            server_time_ms=server_ms,
            clock_skew_ms=abs(observed_ms - server_ms),
            position_mode=self.fetch_position_mode(),
            positions=positions,
            open_orders=tuple(open_orders),
            recent_fills=tuple(recent_fills),
        )

    def check_account(self, trades: list[Any]) -> BinanceReadonlyCheck:
        managed_pair = self._config.managed_pair
        if not managed_pair:
            raise OperationalException("check_account requires a managed_pair.")
        # The original read-only API had no account identity and requested one
        # managed pair.  Explicit P2 accounts always perform the full-account
        # safety read so unmanaged exposure cannot be hidden.
        snapshot = self.fetch_snapshot(
            managed_pair=(
                self._config.managed_pair if self._config.account_id == "default" else None
            )
        )
        issues: list[BinanceReadonlyIssue] = []

        if snapshot.position_mode is not self._config.position_mode:
            issues.append(
                BinanceReadonlyIssue(
                    "POSITION_MODE_MISMATCH",
                    "Account mode differs from runtime configuration.",
                )
            )
        if snapshot.clock_skew_ms > self._config.max_clock_skew_ms:
            issues.append(
                BinanceReadonlyIssue(
                    "CLOCK_SKEW_EXCEEDED",
                    f"Clock skew {snapshot.clock_skew_ms} ms exceeds limit.",
                )
            )

        unmanaged_positions = sorted(
            {
                record.symbol
                for record in snapshot.positions
                if not symbols_equivalent(record.symbol, managed_pair)
            }
        )
        if unmanaged_positions:
            issues.append(
                BinanceReadonlyIssue(
                    "UNMANAGED_POSITION",
                    "Nonzero positions outside managed_pair: " + ", ".join(unmanaged_positions),
                )
            )

        managed_remote = tuple(
            record
            for record in snapshot.positions
            if symbols_equivalent(record.symbol, managed_pair)
        )
        for record in managed_remote:
            if self._config.account_id != "default" and record.margin_mode != "cross":
                issues.append(
                    BinanceReadonlyIssue(
                        "MARGIN_MODE_MISMATCH",
                        f"{record.key.position_side.value} is not cross margin.",
                    )
                )
            if (
                self._config.target_leverage is not None
                and record.leverage != self._config.target_leverage
            ):
                issues.append(
                    BinanceReadonlyIssue(
                        "LEVERAGE_MISMATCH",
                        f"{record.key.position_side.value} leverage differs from target.",
                    )
                )

        unmanaged_orders = sorted(
            {
                str(_value(order, "symbol"))
                for order in snapshot.open_orders
                if _value(order, "symbol")
                and not symbols_equivalent(str(_value(order, "symbol")), managed_pair)
            }
        )
        if unmanaged_orders:
            issues.append(
                BinanceReadonlyIssue(
                    "UNMANAGED_ORDER",
                    "Open orders outside managed_pair: " + ", ".join(unmanaged_orders),
                )
            )

        local_records = records_from_trades(
            trades,
            exchange="binance",
            account_id=self._config.account_id,
            managed_pair=managed_pair,
        )
        local_managed = tuple(
            record for record in local_records if symbols_equivalent(record.symbol, managed_pair)
        )
        return BinanceReadonlyCheck(
            managed_pair=managed_pair,
            configured_position_mode=self._config.position_mode,
            snapshot=snapshot,
            reconciliation=reconcile_positions(local_managed, managed_remote),
            issues=tuple(issues),
        )

    def assess_account(self, trades: list[Any]) -> BinanceRiskAssessment:
        account = self.fetch_account_risk()
        check = self.check_account(trades)
        reasons = [issue.code for issue in check.issues]
        reasons.extend(issue.code for issue in check.reconciliation.issues)
        if account.available_balance > account.equity:
            reasons.append("INVALID_AVAILABLE_BALANCE")
        if account.initial_margin + account.maintenance_margin > account.equity:
            reasons.append("INSUFFICIENT_MARGIN_COVERAGE")
        state = BinanceReadonlyState.HALT if reasons else BinanceReadonlyState.CONSISTENT
        return BinanceRiskAssessment(
            account=account,
            check=check,
            state=state,
            halt_reasons=tuple(dict.fromkeys(reasons)),
        )
