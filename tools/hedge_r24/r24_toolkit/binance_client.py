from __future__ import annotations

import hashlib
import hmac
import json
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode, urlsplit

from .common import redact_url
from .security import Credentials


FAPI_BASE_URL = "https://fapi.binance.com"
SAPI_BASE_URL = "https://api.binance.com"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RECV_WINDOW_MS = 60_000

PUBLIC_PATHS = frozenset({"/fapi/v1/time", "/fapi/v1/exchangeInfo"})
SIGNED_FAPI_PATHS = frozenset(
    {
        "/fapi/v1/accountConfig",
        "/fapi/v1/apiTradingStatus",
        "/fapi/v1/allOrders",
        "/fapi/v1/commissionRate",
        "/fapi/v1/income",
        "/fapi/v1/leverageBracket",
        "/fapi/v1/multiAssetsMargin",
        "/fapi/v1/openOrders",
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/rateLimit/order",
        "/fapi/v1/symbolConfig",
        "/fapi/v1/userTrades",
        "/fapi/v2/positionRisk",
        "/fapi/v3/account",
        "/fapi/v3/balance",
        "/fapi/v3/positionRisk",
    }
)
SIGNED_SAPI_PATHS = frozenset({"/sapi/v1/account/apiRestrictions"})


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    payload: bytes


class Transport(Protocol):
    def request(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse: ...


class BinanceApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        path: str,
        status: int | None = None,
        code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.status = status
        self.code = code
        self.retryable = retryable


class UrlLibTransport:
    def __init__(self, proxy_url: str) -> None:
        parsed = urlsplit(proxy_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("Proxy URL must be http(s)://host:port")
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )

    def request(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise BinanceApiError(
                        f"Response exceeded {MAX_RESPONSE_BYTES} bytes",
                        path=urlsplit(url).path,
                    )
                return HttpResponse(int(response.status), dict(response.headers.items()), payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_RESPONSE_BYTES + 1)
            return HttpResponse(int(exc.code), dict(exc.headers.items()), payload)


@dataclass(frozen=True)
class ClockSample:
    server_time_ms: int
    local_midpoint_ms: float
    round_trip_ms: float
    offset_ms: float


@dataclass(frozen=True)
class ClockProfile:
    samples: tuple[ClockSample, ...]
    offset_ms: int
    median_rtt_ms: float
    max_rtt_ms: float
    offset_spread_ms: float

    def to_summary(self) -> dict[str, Any]:
        return {
            "sample_count": len(self.samples),
            "offset_ms": self.offset_ms,
            "median_rtt_ms": round(self.median_rtt_ms, 3),
            "max_rtt_ms": round(self.max_rtt_ms, 3),
            "offset_spread_ms": round(self.offset_spread_ms, 3),
            "server_time_ms": self.samples[-1].server_time_ms,
        }


class BinanceUSDMReadonlyClient:
    """Allowlisted Binance client which exposes GET only and never logs credentials."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        proxy_url: str,
        timeout: float = 25.0,
        max_retries: int = 4,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        credentials.validate()
        self._credentials = credentials
        self._transport = transport or UrlLibTransport(proxy_url)
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.clock_profile: ClockProfile | None = None
        self.request_count = 0
        self.retry_count = 0
        self.weight_headers: dict[str, str] = {}

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    def _decode(self, response: HttpResponse, *, path: str) -> Any:
        try:
            value = json.loads(response.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BinanceApiError(
                f"Binance returned invalid JSON for {path}", path=path, status=response.status
            ) from exc
        if response.status >= 400:
            code = value.get("code") if isinstance(value, dict) else None
            raw_message = value.get("msg", "request failed") if isinstance(value, dict) else "request failed"
            retryable = response.status in {418, 429} or response.status >= 500 or code in {
                -1001,
                -1003,
                -1021,
            }
            raise BinanceApiError(
                f"Binance GET {path} failed: HTTP {response.status}, code={code}, {raw_message}",
                path=path,
                status=response.status,
                code=int(code) if isinstance(code, int) else None,
                retryable=retryable,
            )
        return value

    def _request_json(
        self,
        base_url: str,
        path: str,
        query: str | Callable[[], str],
        *,
        authenticated: bool,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": "freqtrade-hedge-r24-readonly/2.4.1",
        }
        if authenticated:
            headers["X-MBX-APIKEY"] = self._credentials.api_key

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            query_value = query() if callable(query) else query
            url = f"{base_url}{path}"
            if query_value:
                url = f"{url}?{query_value}"
            try:
                self.request_count += 1
                response = self._transport.request(url, headers, self._timeout)
                for name, value in response.headers.items():
                    if name.lower().startswith("x-mbx-used-weight") or name.lower().startswith(
                        "x-mbx-order-count"
                    ):
                        self.weight_headers[name.lower()] = str(value)
                value = self._decode(response, path=path)
                return value
            except BinanceApiError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                if exc.code == -1021:
                    self.sync_clock(3)
                retry_after = 0.0
                if "response" in locals():
                    for name, value in response.headers.items():
                        if name.lower() == "retry-after":
                            try:
                                retry_after = min(30.0, max(0.0, float(value)))
                            except ValueError:
                                retry_after = 0.0
                delay = retry_after or min(8.0, 0.5 * (2**attempt))
                self.retry_count += 1
                self._sleep(delay)
            except Exception as exc:  # network boundary; URL must be redacted
                last_error = exc
                if attempt >= self._max_retries:
                    safe_url = redact_url(url)
                    raise BinanceApiError(
                        f"Binance GET transport failure at {safe_url}: {type(exc).__name__}",
                        path=path,
                    ) from exc
                self.retry_count += 1
                self._sleep(min(8.0, 0.5 * (2**attempt)))
        raise RuntimeError(f"Unreachable request state: {type(last_error).__name__}")

    def public_get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if path not in PUBLIC_PATHS:
            raise ValueError(f"Public endpoint is not allowlisted for R2.4: {path}")
        return self._request_json(FAPI_BASE_URL, path, urlencode(params or {}), authenticated=False)

    def sync_clock(self, sample_count: int = 5) -> ClockProfile:
        if sample_count < 3 or sample_count > 15:
            raise ValueError("Clock sample count must be between 3 and 15")
        samples: list[ClockSample] = []
        for _ in range(sample_count):
            before = self._now_ms()
            payload = self.public_get("/fapi/v1/time")
            after = self._now_ms()
            if not isinstance(payload, dict) or not isinstance(payload.get("serverTime"), int):
                raise BinanceApiError("Invalid server time payload", path="/fapi/v1/time")
            midpoint = (before + after) / 2
            samples.append(
                ClockSample(
                    server_time_ms=int(payload["serverTime"]),
                    local_midpoint_ms=midpoint,
                    round_trip_ms=float(after - before),
                    offset_ms=float(payload["serverTime"] - midpoint),
                )
            )
        best = sorted(samples, key=lambda item: item.round_trip_ms)[: max(3, len(samples) // 2)]
        offset = int(round(statistics.median(item.offset_ms for item in best)))
        offsets = [item.offset_ms for item in samples]
        profile = ClockProfile(
            samples=tuple(samples),
            offset_ms=offset,
            median_rtt_ms=float(statistics.median(item.round_trip_ms for item in samples)),
            max_rtt_ms=max(item.round_trip_ms for item in samples),
            offset_spread_ms=max(offsets) - min(offsets),
        )
        self.clock_profile = profile
        return profile

    def _signed_query(self, params: Mapping[str, Any] | None) -> str:
        if self.clock_profile is None:
            raise RuntimeError("sync_clock() must pass before an authenticated request")
        query_params = {key: value for key, value in (params or {}).items() if value is not None}
        dynamic_recv_window = min(
            MAX_RECV_WINDOW_MS,
            max(5_000, abs(self.clock_profile.offset_ms) + int(self.clock_profile.max_rtt_ms * 4) + 2_000),
        )
        query_params["recvWindow"] = dynamic_recv_window
        query_params["timestamp"] = self._now_ms() + self.clock_profile.offset_ms
        unsigned = urlencode(query_params)
        signature = hmac.new(
            self._credentials.api_secret.encode("utf-8"),
            unsigned.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{unsigned}&signature={signature}"

    def signed_get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if path in SIGNED_FAPI_PATHS:
            base = FAPI_BASE_URL
        elif path in SIGNED_SAPI_PATHS:
            base = SAPI_BASE_URL
        else:
            raise ValueError(f"Signed endpoint is not allowlisted for R2.4: {path}")
        return self._request_json(
            base,
            path,
            lambda: self._signed_query(params),
            authenticated=True,
        )

    def account_fingerprint(self, account_alias: str) -> str:
        material = f"r24-account:{account_alias}:{self._credentials.api_key}".encode("utf-8")
        return hmac.new(
            self._credentials.api_secret.encode("utf-8"), material, hashlib.sha256
        ).hexdigest()[:24]
