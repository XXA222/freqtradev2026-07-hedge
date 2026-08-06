from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping


ZERO = Decimal("0")
R5_ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT")


class R5Stage(StrEnum):
    MAINNET_READONLY = "MAINNET_READONLY"
    ORDER_TEST = "ORDER_TEST"
    SUBMIT_CANCEL = "SUBMIT_CANCEL"
    SINGLE_LEG = "SINGLE_LEG"
    DUAL_LEG = "DUAL_LEG"
    RESTRICTED_AUTO = "RESTRICTED_AUTO"
    EXPANDED_LIMITS = "EXPANDED_LIMITS"

    @property
    def rank(self) -> int:
        return _STAGE_ORDER.index(self)

    @property
    def previous(self) -> R5Stage | None:
        return None if self.rank == 0 else _STAGE_ORDER[self.rank - 1]

    @property
    def performs_exchange_write(self) -> bool:
        return self is not R5Stage.MAINNET_READONLY

    @property
    def can_create_position(self) -> bool:
        return self.rank >= R5Stage.SINGLE_LEG.rank


_STAGE_ORDER = tuple(R5Stage)


class R5RunState(StrEnum):
    NEW = "NEW"
    PREFLIGHT = "PREFLIGHT"
    ARMED = "ARMED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

    @property
    def terminal(self) -> bool:
        return self in {
            R5RunState.COMPLETED,
            R5RunState.BLOCKED,
            R5RunState.FAILED,
            R5RunState.RECOVERY_REQUIRED,
        }


def exact_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must use an exact decimal")
    try:
        result = Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class R5LimitProfile:
    stage: R5Stage
    max_single_order_notional: Decimal
    max_gross_notional: Decimal
    max_net_notional: Decimal
    max_daily_loss: Decimal
    max_daily_turnover: Decimal
    max_daily_orders: int
    max_open_orders: int
    max_runtime_minutes: int
    max_slippage_bps: int
    write_request_budget: int
    require_flat_start: bool = True
    require_flat_finish: bool = True

    def __post_init__(self) -> None:
        stage = self.stage if isinstance(self.stage, R5Stage) else R5Stage(self.stage)
        names = (
            "max_single_order_notional",
            "max_gross_notional",
            "max_net_notional",
            "max_daily_loss",
            "max_daily_turnover",
        )
        values = {name: exact_decimal(getattr(self, name), name) for name in names}
        if any(value < ZERO for value in values.values()):
            raise ValueError("R5 monetary limits cannot be negative")
        if values["max_single_order_notional"] > Decimal("250"):
            raise ValueError("R5 hard cap: single order cannot exceed 250 USDT")
        if values["max_gross_notional"] > Decimal("1000"):
            raise ValueError("R5 hard cap: gross notional cannot exceed 1000 USDT")
        if values["max_net_notional"] > values["max_gross_notional"]:
            raise ValueError("max_net_notional cannot exceed max_gross_notional")
        if values["max_daily_loss"] > Decimal("50"):
            raise ValueError("R5 hard cap: daily loss cannot exceed 50 USDT")
        if values["max_daily_turnover"] > Decimal("10000"):
            raise ValueError("R5 hard cap: daily turnover cannot exceed 10000 USDT")
        for name, maximum in (
            ("max_daily_orders", 300),
            ("max_open_orders", 50),
            ("max_runtime_minutes", 1440),
            ("write_request_budget", 600),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer in [0, {maximum}]")
        if isinstance(self.max_slippage_bps, bool) or not isinstance(self.max_slippage_bps, int):
            raise ValueError("max_slippage_bps must be an integer")
        if not 0 <= self.max_slippage_bps <= 50:
            raise ValueError("R5 hard cap: max_slippage_bps must be in [0, 50]")
        if not isinstance(self.require_flat_start, bool) or not isinstance(self.require_flat_finish, bool):
            raise TypeError("flat requirements must be booleans")
        if stage is R5Stage.MAINNET_READONLY and self.write_request_budget != 0:
            raise ValueError("MAINNET_READONLY cannot have a write budget")
        if stage is R5Stage.ORDER_TEST and self.write_request_budget != 1:
            raise ValueError("ORDER_TEST write budget must equal one")
        if stage.rank < R5Stage.SINGLE_LEG.rank and values["max_gross_notional"] != ZERO:
            raise ValueError("pre-position stages must have zero gross-position budget")
        object.__setattr__(self, "stage", stage)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "max_single_order_notional": str(self.max_single_order_notional),
            "max_gross_notional": str(self.max_gross_notional),
            "max_net_notional": str(self.max_net_notional),
            "max_daily_loss": str(self.max_daily_loss),
            "max_daily_turnover": str(self.max_daily_turnover),
            "max_daily_orders": self.max_daily_orders,
            "max_open_orders": self.max_open_orders,
            "max_runtime_minutes": self.max_runtime_minutes,
            "max_slippage_bps": self.max_slippage_bps,
            "write_request_budget": self.write_request_budget,
            "require_flat_start": self.require_flat_start,
            "require_flat_finish": self.require_flat_finish,
        }


def default_limit_profile(stage: R5Stage | str) -> R5LimitProfile:
    value = stage if isinstance(stage, R5Stage) else R5Stage(stage)
    profiles: dict[R5Stage, dict[str, object]] = {
        R5Stage.MAINNET_READONLY: dict(
            max_single_order_notional="0", max_gross_notional="0", max_net_notional="0",
            max_daily_loss="0", max_daily_turnover="0", max_daily_orders=0, max_open_orders=0,
            max_runtime_minutes=10, max_slippage_bps=0, write_request_budget=0,
        ),
        R5Stage.ORDER_TEST: dict(
            max_single_order_notional="10", max_gross_notional="0", max_net_notional="0",
            max_daily_loss="0", max_daily_turnover="0", max_daily_orders=1, max_open_orders=0,
            max_runtime_minutes=10, max_slippage_bps=0, write_request_budget=1,
        ),
        R5Stage.SUBMIT_CANCEL: dict(
            max_single_order_notional="10", max_gross_notional="0", max_net_notional="0",
            max_daily_loss="1", max_daily_turnover="20", max_daily_orders=3, max_open_orders=1,
            max_runtime_minutes=10, max_slippage_bps=0, write_request_budget=3,
        ),
        R5Stage.SINGLE_LEG: dict(
            max_single_order_notional="12", max_gross_notional="15", max_net_notional="15",
            max_daily_loss="2", max_daily_turnover="60", max_daily_orders=8, max_open_orders=2,
            max_runtime_minutes=15, max_slippage_bps=10, write_request_budget=8,
        ),
        R5Stage.DUAL_LEG: dict(
            max_single_order_notional="12", max_gross_notional="25", max_net_notional="15",
            max_daily_loss="3", max_daily_turnover="120", max_daily_orders=16, max_open_orders=4,
            max_runtime_minutes=20, max_slippage_bps=10, write_request_budget=16,
        ),
        R5Stage.RESTRICTED_AUTO: dict(
            max_single_order_notional="25", max_gross_notional="100", max_net_notional="50",
            max_daily_loss="10", max_daily_turnover="500", max_daily_orders=30, max_open_orders=8,
            max_runtime_minutes=60, max_slippage_bps=15, write_request_budget=60,
            require_flat_finish=False,
        ),
        R5Stage.EXPANDED_LIMITS: dict(
            max_single_order_notional="50", max_gross_notional="250", max_net_notional="100",
            max_daily_loss="20", max_daily_turnover="2000", max_daily_orders=100, max_open_orders=20,
            max_runtime_minutes=240, max_slippage_bps=20, write_request_budget=200,
            require_flat_finish=False,
        ),
    }
    return R5LimitProfile(stage=value, **profiles[value])


@dataclass(frozen=True, slots=True)
class R5AccountSnapshot:
    account_id: str
    equity: Decimal
    long_notional: Decimal
    short_notional: Decimal
    open_orders: int
    unmanaged_positions: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        account = str(self.account_id).strip()
        if not account.startswith("binance-usdm:"):
            raise ValueError("R5 account_id must use the mainnet binance-usdm namespace")
        values = {
            "equity": exact_decimal(self.equity, "equity"),
            "long_notional": exact_decimal(self.long_notional, "long_notional"),
            "short_notional": exact_decimal(self.short_notional, "short_notional"),
        }
        if values["equity"] < ZERO or values["long_notional"] < ZERO or values["short_notional"] < ZERO:
            raise ValueError("account snapshot values cannot be negative")
        if isinstance(self.open_orders, bool) or not isinstance(self.open_orders, int) or self.open_orders < 0:
            raise ValueError("open_orders must be a non-negative integer")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        unmanaged = tuple(sorted({str(item).strip() for item in self.unmanaged_positions if str(item).strip()}))
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "unmanaged_positions", unmanaged)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def gross_notional(self) -> Decimal:
        return self.long_notional + self.short_notional

    @property
    def net_notional(self) -> Decimal:
        return abs(self.long_notional - self.short_notional)

    @property
    def flat(self) -> bool:
        return self.gross_notional == ZERO and self.open_orders == 0


@dataclass(frozen=True, slots=True)
class R5StageReport:
    run_id: str
    stage: R5Stage
    state: R5RunState
    account_id: str
    actor: str
    checks: Mapping[str, bool]
    evidence: Mapping[str, Any]
    writes_attempted: int
    real_mainnet_order_requests_sent: int
    started_at: datetime
    completed_at: datetime
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "toolkit_version": "5.1.6",
            "run_id": self.run_id,
            "stage": self.stage.value,
            "state": self.state.value,
            "status": "PASS" if self.state is R5RunState.COMPLETED else self.state.value,
            "account_id": self.account_id,
            "actor": self.actor,
            "checks": {key: "PASS" if value else "FAIL" for key, value in sorted(self.checks.items())},
            "evidence": dict(self.evidence),
            "writes_attempted": self.writes_attempted,
            "real_mainnet_order_requests_sent": self.real_mainnet_order_requests_sent,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": (self.completed_at - self.started_at).total_seconds(),
            "error": self.error,
            "allowed_symbols": list(R5_ALLOWED_SYMBOLS),
            "market_orders": "DISABLED",
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
        return payload
