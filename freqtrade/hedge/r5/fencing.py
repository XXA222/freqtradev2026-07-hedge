"""Fencing-token enforcement at the final exchange side-effect boundary."""

from __future__ import annotations

from typing import Sequence

from freqtrade.hedge.concurrency.single_writer import SingleWriterGuard
from freqtrade.hedge.execution.service import (
    ApprovedOrderIntent,
    ExchangeExecutionPort,
    ExternalOrderSnapshot,
)


class R5FencedExchangeExecutionPort:
    """Recheck the exact lease token immediately before every exchange write."""

    def __init__(
        self,
        delegate: ExchangeExecutionPort,
        *,
        writer: SingleWriterGuard,
    ) -> None:
        self._delegate = delegate
        self._writer = writer
        self._expected_token: int | None = None

    @property
    def delegate(self) -> ExchangeExecutionPort:
        return self._delegate

    def bind_fencing_token(self, token: int) -> None:
        if isinstance(token, bool) or not isinstance(token, int) or token <= 0:
            raise ValueError("fencing token must be a positive integer")
        current = self._writer.assert_valid()
        if current.fencing_token != token:
            raise PermissionError("R5 fencing token is not current")
        self._expected_token = token

    def clear_fencing_token(self) -> None:
        self._expected_token = None

    def _assert_write_fence(self) -> None:
        if self._expected_token is None:
            raise PermissionError("R5 exchange write has no bound fencing token")
        current = self._writer.assert_valid()
        if current.fencing_token != self._expected_token:
            raise PermissionError("R5 single-writer fencing token changed")

    def submit_order(self, approved: ApprovedOrderIntent) -> ExternalOrderSnapshot:
        self._assert_write_fence()
        return self._delegate.submit_order(approved)

    def validate_order(self, approved: ApprovedOrderIntent):
        """Run Binance /order/test under the same final fencing check."""
        self._assert_write_fence()
        validate = getattr(self._delegate, "validate_order", None)
        if not callable(validate):
            raise NotImplementedError("exchange adapter does not support order validation")
        return validate(approved)

    def cancel_order(self, *, client_order_id: str) -> ExternalOrderSnapshot:
        self._assert_write_fence()
        return self._delegate.cancel_order(client_order_id=client_order_id)

    def query_order(self, *, client_order_id: str) -> ExternalOrderSnapshot | None:
        return self._delegate.query_order(client_order_id=client_order_id)

    def list_open_orders(
        self, *, account_id: str, symbol: str
    ) -> Sequence[ExternalOrderSnapshot]:
        return self._delegate.list_open_orders(account_id=account_id, symbol=symbol)

    def list_recent_fills(
        self, *, account_id: str, symbol: str
    ) -> Sequence[ExternalOrderSnapshot]:
        return self._delegate.list_recent_fills(account_id=account_id, symbol=symbol)
