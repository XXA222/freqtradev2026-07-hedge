from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class R4Mode(StrEnum):
    DRY_RUN = "DRY_RUN"
    TESTNET = "TESTNET"
    PRODUCTION_LOCKED = "PRODUCTION_LOCKED"


class RuntimePhase(StrEnum):
    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class ComponentState(StrEnum):
    UNKNOWN = "UNKNOWN"
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    state: ComponentState
    required: bool = True
    detail: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state is ComponentState.READY


@dataclass(frozen=True, slots=True)
class R4RuntimeConfig:
    project_root: Path
    mode: R4Mode = R4Mode.PRODUCTION_LOCKED
    account_id: str = "binance-usdm:default"
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    state_dir: Path | None = None
    database_path: Path | None = None
    config_path: Path | None = None
    metrics_path: Path | None = None
    events_path: Path | None = None
    heartbeat_seconds: int = 5
    reconciliation_max_age_seconds: int = 30
    user_stream_max_age_seconds: int = 30
    market_data_max_age_seconds: int = 120
    planner_enabled: bool = True
    control_plane_enabled: bool = True
    testnet_validation_only: bool = True
    mainnet_writes_enabled: bool = False
    allow_market_orders: bool = False
    max_single_order_notional: Decimal = Decimal("25")
    max_gross_wallet_exposure: Decimal = Decimal("0.65")

    def resolved_state_dir(self) -> Path:
        return self.state_dir or self.project_root / "artifacts" / "hedge_r4_runtime"


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    phase: RuntimePhase
    ready: bool
    new_risk_allowed: bool
    blockers: tuple[str, ...]
    components: tuple[ComponentHealth, ...]
    mainnet_live_exchange_write: str = "LOCKED"
    real_mainnet_order_requests_sent: int = 0


@dataclass(frozen=True, slots=True)
class R4CycleReport:
    cycle_id: str
    processed: bool
    reason: str
    planning_result: object | None = None
    writes_attempted: int = 0
    writes_blocked: int = 0
    cancellations_planned: int = 0
    submissions_planned: int = 0
    diagnostics: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    name: str
    status: str
    duration_seconds: float
    exit_code: int
    summary: str
    stdout_path: str = ""
    stderr_path: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiagnosticSummary:
    started_at: datetime
    completed_at: datetime
    results: tuple[DiagnosticResult, ...]
    output_dir: Path

    @property
    def failed(self) -> tuple[DiagnosticResult, ...]:
        return tuple(item for item in self.results if item.status != "PASS")

    @property
    def passed(self) -> bool:
        return not self.failed
