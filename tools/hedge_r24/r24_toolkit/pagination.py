from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .common import chunks_by_time


SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class PageResult:
    rows: tuple[dict[str, Any], ...]
    request_count: int
    duplicate_count: int
    complete: bool
    oldest_time_ms: int | None
    newest_time_ms: int | None

    def watermark(self) -> dict[str, Any]:
        return {
            "row_count": len(self.rows),
            "request_count": self.request_count,
            "duplicate_count": self.duplicate_count,
            "complete": self.complete,
            "oldest_time_ms": self.oldest_time_ms,
            "newest_time_ms": self.newest_time_ms,
        }


def _deduplicate(
    rows: list[dict[str, Any]], key: Callable[[Mapping[str, Any]], str]
) -> tuple[list[dict[str, Any]], int]:
    unique: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for row in rows:
        natural_key = key(row)
        if natural_key in unique:
            duplicate_count += 1
            if unique[natural_key] != row:
                raise ValueError(f"Conflicting duplicate row for key {natural_key}")
        else:
            unique[natural_key] = row
    return list(unique.values()), duplicate_count


def _result(
    rows: list[dict[str, Any]],
    request_count: int,
    key: Callable[[Mapping[str, Any]], str],
    time_field: str,
) -> PageResult:
    unique, duplicates = _deduplicate(rows, key)
    unique.sort(key=lambda row: (int(row.get(time_field, 0)), key(row)))
    times = [int(row[time_field]) for row in unique if row.get(time_field) is not None]
    return PageResult(
        rows=tuple(unique),
        request_count=request_count,
        duplicate_count=duplicates,
        complete=True,
        oldest_time_ms=min(times) if times else None,
        newest_time_ms=max(times) if times else None,
    )


def fetch_all_orders(
    get: Callable[[str, Mapping[str, Any]], Any],
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    max_pages: int = 1000,
) -> PageResult:
    rows: list[dict[str, Any]] = []
    request_count = 0
    for window_start, window_end in chunks_by_time(
        start_ms, end_ms, max_window_ms=SEVEN_DAYS_MS
    ):
        next_order_id: int | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "symbol": symbol,
                "startTime": window_start,
                "endTime": window_end,
                "limit": limit,
            }
            if next_order_id is not None:
                params["orderId"] = next_order_id
            page = get("/fapi/v1/allOrders", params)
            request_count += 1
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError("allOrders returned a non-list payload")
            filtered = [
                item
                for item in page
                if window_start <= int(item.get("updateTime", item.get("time", 0))) <= window_end
            ]
            rows.extend(filtered)
            if len(page) < limit:
                break
            order_ids = [int(item["orderId"]) for item in page]
            candidate = max(order_ids) + 1
            if next_order_id is not None and candidate <= next_order_id:
                raise ValueError("allOrders pagination did not advance")
            next_order_id = candidate
        else:
            raise ValueError("allOrders exceeded the pagination safety limit")
    return _result(
        rows,
        request_count,
        lambda item: f"{item.get('symbol')}:{item.get('orderId')}",
        "updateTime",
    )


def fetch_user_trades(
    get: Callable[[str, Mapping[str, Any]], Any],
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    max_pages: int = 1000,
) -> PageResult:
    rows: list[dict[str, Any]] = []
    request_count = 0
    for window_start, window_end in chunks_by_time(
        start_ms, end_ms, max_window_ms=SEVEN_DAYS_MS
    ):
        page = get(
            "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": window_start, "endTime": window_end, "limit": limit},
        )
        request_count += 1
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise ValueError("userTrades returned a non-list payload")
        rows.extend(page)
        if len(page) < limit:
            continue

        next_trade_id = max(int(item["id"]) for item in page) + 1
        for _ in range(max_pages):
            page = get(
                "/fapi/v1/userTrades",
                {"symbol": symbol, "fromId": next_trade_id, "limit": limit},
            )
            request_count += 1
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError("userTrades cursor returned a non-list payload")
            within_window = [item for item in page if int(item.get("time", 0)) <= window_end]
            rows.extend(item for item in within_window if int(item.get("time", 0)) >= window_start)
            if len(page) < limit or not within_window:
                break
            candidate = max(int(item["id"]) for item in page) + 1
            if candidate <= next_trade_id:
                raise ValueError("userTrades pagination did not advance")
            next_trade_id = candidate
        else:
            raise ValueError("userTrades exceeded the pagination safety limit")
    return _result(
        rows,
        request_count,
        lambda item: f"{item.get('symbol')}:{item.get('id')}",
        "time",
    )


def fetch_income(
    get: Callable[[str, Mapping[str, Any]], Any],
    *,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    max_pages: int = 1000,
) -> PageResult:
    rows: list[dict[str, Any]] = []
    request_count = 0
    for window_start, window_end in chunks_by_time(
        start_ms, end_ms, max_window_ms=SEVEN_DAYS_MS
    ):
        page_number = 1
        for _ in range(max_pages):
            page = get(
                "/fapi/v1/income",
                {
                    "startTime": window_start,
                    "endTime": window_end,
                    "page": page_number,
                    "limit": limit,
                },
            )
            request_count += 1
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError("income returned a non-list payload")
            rows.extend(page)
            if len(page) < limit:
                break
            page_number += 1
        else:
            raise ValueError("income exceeded the pagination safety limit")
    return _result(
        rows,
        request_count,
        lambda item: (
            f"{item.get('tranId')}:{item.get('incomeType')}:{item.get('symbol')}:"
            f"{item.get('asset')}:{item.get('time')}"
        ),
        "time",
    )
