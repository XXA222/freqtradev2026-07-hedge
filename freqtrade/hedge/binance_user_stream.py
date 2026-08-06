"""Pure parser for Binance USD-M futures read-only account updates."""

from __future__ import annotations

import warnings

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from freqtrade.enums import PositionSide
from freqtrade.exceptions import OperationalException
from freqtrade.hedge.position_book import PositionRecord, SideAwarePositionBook
from freqtrade.hedge.reconciliation import reconcile_positions


@dataclass(frozen=True, slots=True)
class BinanceBalanceUpdate:
    asset: str
    wallet_balance: Decimal
    cross_wallet_balance: Decimal
    balance_change: Decimal


@dataclass(frozen=True, slots=True)
class BinanceUserStreamUpdate:
    event_time_ms: int
    transaction_time_ms: int
    reason: str
    balances: tuple[BinanceBalanceUpdate, ...]
    positions: tuple[PositionRecord, ...]
    unexpected_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BinanceOrderUpdate:
    event_time_ms: int
    transaction_time_ms: int
    symbol: str
    client_order_id: str
    order_id: int
    side: str
    position_side: PositionSide
    execution_type: str
    order_status: str
    original_quantity: Decimal
    cumulative_filled_quantity: Decimal
    last_filled_quantity: Decimal
    average_price: Decimal
    realized_profit: Decimal


class BinanceStreamState(StrEnum):
    READY = "READY"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class BinanceStreamStatus:
    state: BinanceStreamState
    reasons: tuple[str, ...]
    last_event_time_ms: int | None
    positions: tuple[PositionRecord, ...]
    balances: tuple[BinanceBalanceUpdate, ...]
    orders: tuple[BinanceOrderUpdate, ...]

    @property
    def allows_risk_increase(self) -> bool:
        return self.state is BinanceStreamState.READY


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OperationalException(
            f"Binance ACCOUNT_UPDATE contains invalid {field}."
        ) from exc


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if result < 0:
        raise OperationalException(
            f"Binance user stream contains negative {field}."
        )
    return result


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise OperationalException(
            f"Binance ACCOUNT_UPDATE contains invalid {field}."
        )
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise OperationalException(
            f"Binance ACCOUNT_UPDATE contains invalid {field}."
        ) from exc
    if result < 0:
        raise OperationalException(
            f"Binance ACCOUNT_UPDATE contains negative {field}."
        )
    return result


def _raw_symbol(pair: str) -> str:
    contract = pair.split(":", maxsplit=1)[0]
    return contract.replace("/", "")


def parse_binance_account_update(
    event: Mapping[str, Any],
    managed_pair: str,
) -> BinanceUserStreamUpdate:
    if event.get("e") != "ACCOUNT_UPDATE":
        raise OperationalException("Expected a Binance ACCOUNT_UPDATE event.")
    account = event.get("a")
    if not isinstance(account, Mapping):
        raise OperationalException("Binance ACCOUNT_UPDATE is missing account data.")

    raw_balances = account.get("B")
    raw_positions = account.get("P")
    if not isinstance(raw_balances, list) or not all(
        isinstance(item, Mapping) for item in raw_balances
    ):
        raise OperationalException("Binance ACCOUNT_UPDATE balances must be a list.")
    if not isinstance(raw_positions, list) or not all(
        isinstance(item, Mapping) for item in raw_positions
    ):
        raise OperationalException("Binance ACCOUNT_UPDATE positions must be a list.")

    balances = tuple(
        BinanceBalanceUpdate(
            asset=str(item.get("a", "")),
            wallet_balance=_decimal(item.get("wb"), "wallet balance"),
            cross_wallet_balance=_decimal(item.get("cw"), "cross wallet balance"),
            balance_change=_decimal(item.get("bc"), "balance change"),
        )
        for item in raw_balances
    )
    if any(not balance.asset for balance in balances):
        raise OperationalException("Binance ACCOUNT_UPDATE balance asset is missing.")

    expected_symbol = _raw_symbol(managed_pair)
    positions: list[PositionRecord] = []
    unexpected_symbols: set[str] = set()
    for item in raw_positions:
        if item.get("s") != expected_symbol:
            if _decimal(item.get("pa"), "position amount") != 0:
                unexpected_symbols.add(str(item.get("s", "")))
            continue
        raw_side = item.get("ps")
        if raw_side not in (PositionSide.LONG.value, PositionSide.SHORT.value):
            raise OperationalException(
                "Binance hedge ACCOUNT_UPDATE requires LONG or SHORT position side."
            )
        amount = _decimal(item.get("pa"), "position amount")
        positions.append(
            PositionRecord(
                symbol=managed_pair,
                position_side=raw_side,
                amount=abs(amount),
                entry_price=_decimal(item.get("ep"), "entry price"),
                collateral=_decimal(item.get("iw", 0), "isolated wallet"),
                source="binance_user_stream",
            )
        )

    return BinanceUserStreamUpdate(
        event_time_ms=_integer(event.get("E"), "event time"),
        transaction_time_ms=_integer(event.get("T"), "transaction time"),
        reason=str(account.get("m", "")),
        balances=balances,
        positions=tuple(sorted(positions, key=lambda record: record.key)),
        unexpected_symbols=tuple(sorted(unexpected_symbols)),
    )


def parse_binance_order_update(
    event: Mapping[str, Any],
    managed_pair: str,
) -> BinanceOrderUpdate:
    if event.get("e") != "ORDER_TRADE_UPDATE":
        raise OperationalException("Expected a Binance ORDER_TRADE_UPDATE event.")
    order = event.get("o")
    if not isinstance(order, Mapping):
        raise OperationalException("Binance ORDER_TRADE_UPDATE is missing order data.")
    if order.get("s") != _raw_symbol(managed_pair):
        raise OperationalException(
            "Binance ORDER_TRADE_UPDATE is outside managed_pair."
        )

    client_order_id = order.get("c")
    if not isinstance(client_order_id, str) or not client_order_id:
        raise OperationalException(
            "Binance ORDER_TRADE_UPDATE is missing client order id."
        )
    side = order.get("S")
    if side not in ("BUY", "SELL"):
        raise OperationalException("Binance ORDER_TRADE_UPDATE contains invalid side.")
    raw_position_side = order.get("ps")
    if raw_position_side not in (PositionSide.LONG.value, PositionSide.SHORT.value):
        raise OperationalException(
            "Binance hedge ORDER_TRADE_UPDATE requires LONG or SHORT position side."
        )

    original_quantity = _nonnegative_decimal(order.get("q"), "original quantity")
    cumulative_filled_quantity = _nonnegative_decimal(
        order.get("z"),
        "cumulative filled quantity",
    )
    last_filled_quantity = _nonnegative_decimal(
        order.get("l"),
        "last filled quantity",
    )
    if (
        cumulative_filled_quantity > original_quantity
        or last_filled_quantity > cumulative_filled_quantity
    ):
        raise OperationalException(
            "Binance ORDER_TRADE_UPDATE contains impossible fill quantities."
        )
    execution_type = order.get("x")
    order_status = order.get("X")
    if not isinstance(execution_type, str) or not execution_type:
        raise OperationalException(
            "Binance ORDER_TRADE_UPDATE is missing execution type."
        )
    if not isinstance(order_status, str) or not order_status:
        raise OperationalException(
            "Binance ORDER_TRADE_UPDATE is missing order status."
        )

    return BinanceOrderUpdate(
        event_time_ms=_integer(event.get("E"), "event time"),
        transaction_time_ms=_integer(event.get("T"), "transaction time"),
        symbol=managed_pair,
        client_order_id=client_order_id,
        order_id=_integer(order.get("i"), "order id"),
        side=side,
        position_side=PositionSide(raw_position_side),
        execution_type=execution_type,
        order_status=order_status,
        original_quantity=original_quantity,
        cumulative_filled_quantity=cumulative_filled_quantity,
        last_filled_quantity=last_filled_quantity,
        average_price=_nonnegative_decimal(order.get("ap"), "average price"),
        realized_profit=_decimal(order.get("rp"), "realized profit"),
    )


class BinanceUserStreamMirror:
    """Maintain a read-only partial-update mirror bootstrapped from REST."""

    def __init__(self, managed_pair: str) -> None:
        warnings.warn(
            "Legacy Hedge Binance compatibility stack is deprecated; "
            "use freqtrade.hedge.exchange and freqtrade.hedge.readonly.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._managed_pair = managed_pair
        self._positions = SideAwarePositionBook()
        self._balances: dict[str, BinanceBalanceUpdate] = {}
        self._orders: dict[str, BinanceOrderUpdate] = {}
        self._last_event_time_ms: int | None = None
        self._halt_reasons: list[str] = ["REST_BOOTSTRAP_REQUIRED"]

    def bootstrap(
        self,
        positions: tuple[PositionRecord, ...],
        *,
        observed_at_ms: int,
    ) -> None:
        if observed_at_ms < 0:
            raise OperationalException("REST bootstrap timestamp must not be negative.")
        self._positions = SideAwarePositionBook(positions)
        self._last_event_time_ms = observed_at_ms
        self._halt_reasons = []

    def apply(self, update: BinanceUserStreamUpdate) -> None:
        if self._last_event_time_ms is None:
            self._add_halt("REST_BOOTSTRAP_REQUIRED")
            return
        if update.event_time_ms < self._last_event_time_ms:
            self._add_halt("OUT_OF_ORDER_EVENT")
            return
        if update.unexpected_symbols:
            self._add_halt("UNMANAGED_POSITION")

        for balance in update.balances:
            self._balances[balance.asset] = balance
        for position in update.positions:
            if position.symbol != self._managed_pair:
                self._add_halt("UNMANAGED_POSITION")
                continue
            if position.amount == 0:
                self._positions.remove(position.symbol, position.position_side)
            else:
                self._positions.upsert(position)
        self._last_event_time_ms = update.event_time_ms

    def disconnect(self) -> None:
        self._add_halt("USER_STREAM_DISCONNECTED")

    def apply_order(
        self,
        update: BinanceOrderUpdate,
        *,
        known_client_order_ids: set[str],
    ) -> None:
        if self._last_event_time_ms is None:
            self._add_halt("REST_BOOTSTRAP_REQUIRED")
            return
        if update.event_time_ms < self._last_event_time_ms:
            self._add_halt("OUT_OF_ORDER_EVENT")
            return
        if update.symbol != self._managed_pair:
            self._add_halt("UNMANAGED_ORDER")
            return
        if update.client_order_id not in known_client_order_ids:
            self._add_halt("UNKNOWN_ORDER")
        self._orders[update.client_order_id] = update
        self._last_event_time_ms = update.event_time_ms

    def calibrate_rest(self, positions: tuple[PositionRecord, ...]) -> None:
        result = reconcile_positions(self._positions.all(), positions)
        if not result.consistent:
            self._add_halt("REST_POSITION_DRIFT")

    def status(
        self,
        *,
        now_ms: int,
        max_age_ms: int,
    ) -> BinanceStreamStatus:
        if max_age_ms <= 0:
            raise OperationalException("max_age_ms must be positive.")
        reasons = list(self._halt_reasons)
        if self._last_event_time_ms is None or now_ms - self._last_event_time_ms > max_age_ms:
            reasons.append("STALE_USER_STREAM")
        if now_ms < (self._last_event_time_ms or 0):
            reasons.append("CLOCK_BEFORE_LAST_EVENT")
        reasons = list(dict.fromkeys(reasons))
        return BinanceStreamStatus(
            state=BinanceStreamState.HALT if reasons else BinanceStreamState.READY,
            reasons=tuple(reasons),
            last_event_time_ms=self._last_event_time_ms,
            positions=self._positions.all(),
            balances=tuple(self._balances[key] for key in sorted(self._balances)),
            orders=tuple(self._orders[key] for key in sorted(self._orders)),
        )

    def _add_halt(self, reason: str) -> None:
        if reason not in self._halt_reasons:
            self._halt_reasons.append(reason)
