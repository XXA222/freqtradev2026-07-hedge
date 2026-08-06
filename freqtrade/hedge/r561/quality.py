"""Seven-layer distinction between process liveness and credible Dry-run output."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

from .network import NetworkProbeReport


class RuntimeQualityState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING_UNVERIFIED = "RUNNING_UNVERIFIED"
    DATA_STALE = "DATA_STALE"
    STRATEGY_STALE = "STRATEGY_STALE"
    FUNCTION_UNVERIFIED = "FUNCTION_UNVERIFIED"
    LEDGER_UNVERIFIED = "LEDGER_UNVERIFIED"
    UI_UNVERIFIED = "UI_UNVERIFIED"
    CREDIBLE = "CREDIBLE"


class RuntimeLayer(IntEnum):
    PROCESS = 1
    EXCHANGE = 2
    DATA = 3
    STRATEGY = 4
    SIMULATION = 5
    LEDGER_RECOVERY = 6
    UI_API = 7


@dataclass(frozen=True, slots=True)
class RuntimeCredibilityEvidence:
    observed_at: datetime
    process_running: bool
    exchange_initialized: bool
    ticker_probe: NetworkProbeReport | None = None
    ohlcv_probe: NetworkProbeReport | None = None
    last_strategy_cycle_at: datetime | None = None
    strategy_cycle_count: int = 0
    order_count: int = 0
    fill_count: int = 0
    deterministic_scenario_passed: bool = False
    ledger_consistent: bool = False
    restart_recovered: bool = False
    reconciliation_at: datetime | None = None
    api_healthy: bool = False
    dashboard_healthy: bool = False


@dataclass(frozen=True, slots=True)
class LayerResult:
    layer: RuntimeLayer
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeCredibilityAssessment:
    state: RuntimeQualityState
    highest_passed_layer: int
    credible: bool
    layers: tuple[LayerResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "highest_passed_layer": self.highest_passed_layer,
            "credible": self.credible,
            "layers": [
                {
                    "layer": int(item.layer),
                    "name": item.layer.name,
                    "passed": item.passed,
                    "reasons": item.reasons,
                }
                for item in self.layers
            ],
        }


def evaluate_runtime_credibility(
    evidence: RuntimeCredibilityEvidence,
    *,
    max_strategy_cycle_age_seconds: int = 120,
    max_reconciliation_age_seconds: int = 180,
) -> RuntimeCredibilityAssessment:
    if evidence.observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    layers: list[LayerResult] = []
    layers.append(
        LayerResult(
            RuntimeLayer.PROCESS,
            evidence.process_running,
            () if evidence.process_running else ("PROCESS_NOT_RUNNING",),
        )
    )
    layers.append(
        LayerResult(
            RuntimeLayer.EXCHANGE,
            evidence.exchange_initialized,
            () if evidence.exchange_initialized else ("EXCHANGE_NOT_INITIALIZED",),
        )
    )
    data_reasons: list[str] = []
    if evidence.ticker_probe is None or not evidence.ticker_probe.ready:
        data_reasons.append("TICKER_PROBE_NOT_READY")
    if evidence.ohlcv_probe is None or not evidence.ohlcv_probe.ready:
        data_reasons.append("OHLCV_PROBE_NOT_READY")
    layers.append(LayerResult(RuntimeLayer.DATA, not data_reasons, tuple(data_reasons)))
    strategy_reasons: list[str] = []
    if evidence.strategy_cycle_count < 1:
        strategy_reasons.append("NO_STRATEGY_CYCLE")
    if evidence.last_strategy_cycle_at is None:
        strategy_reasons.append("STRATEGY_CYCLE_TIMESTAMP_MISSING")
    else:
        if evidence.last_strategy_cycle_at.tzinfo is None:
            strategy_reasons.append("STRATEGY_CYCLE_TIMESTAMP_NAIVE")
        elif (
            evidence.observed_at - evidence.last_strategy_cycle_at
        ).total_seconds() > max_strategy_cycle_age_seconds:
            strategy_reasons.append("STRATEGY_CYCLE_STALE")
    layers.append(
        LayerResult(RuntimeLayer.STRATEGY, not strategy_reasons, tuple(strategy_reasons))
    )
    simulation_reasons: list[str] = []
    if not evidence.deterministic_scenario_passed:
        simulation_reasons.append("DETERMINISTIC_SCENARIO_NOT_PASSED")
    if evidence.order_count < 1:
        simulation_reasons.append("NO_SIMULATED_ORDERS")
    if evidence.fill_count < 1:
        simulation_reasons.append("NO_SIMULATED_FILLS")
    layers.append(
        LayerResult(RuntimeLayer.SIMULATION, not simulation_reasons, tuple(simulation_reasons))
    )
    ledger_reasons: list[str] = []
    if not evidence.ledger_consistent:
        ledger_reasons.append("LEDGER_NOT_CONSISTENT")
    if not evidence.restart_recovered:
        ledger_reasons.append("RESTART_RECOVERY_NOT_VERIFIED")
    if evidence.reconciliation_at is None:
        ledger_reasons.append("RECONCILIATION_TIMESTAMP_MISSING")
    else:
        if evidence.reconciliation_at.tzinfo is None:
            ledger_reasons.append("RECONCILIATION_TIMESTAMP_NAIVE")
        elif (
            evidence.observed_at - evidence.reconciliation_at
        ).total_seconds() > max_reconciliation_age_seconds:
            ledger_reasons.append("RECONCILIATION_STALE")
    layers.append(
        LayerResult(RuntimeLayer.LEDGER_RECOVERY, not ledger_reasons, tuple(ledger_reasons))
    )
    ui_reasons: list[str] = []
    if not evidence.api_healthy:
        ui_reasons.append("API_NOT_HEALTHY")
    if not evidence.dashboard_healthy:
        ui_reasons.append("DASHBOARD_NOT_HEALTHY")
    layers.append(LayerResult(RuntimeLayer.UI_API, not ui_reasons, tuple(ui_reasons)))

    highest = 0
    for item in layers:
        if item.passed and int(item.layer) == highest + 1:
            highest = int(item.layer)
        else:
            break
    state_by_layer = {
        0: RuntimeQualityState.STOPPED,
        1: RuntimeQualityState.RUNNING_UNVERIFIED,
        2: RuntimeQualityState.RUNNING_UNVERIFIED,
        3: RuntimeQualityState.STRATEGY_STALE,
        4: RuntimeQualityState.FUNCTION_UNVERIFIED,
        5: RuntimeQualityState.LEDGER_UNVERIFIED,
        6: RuntimeQualityState.UI_UNVERIFIED,
        7: RuntimeQualityState.CREDIBLE,
    }
    if evidence.process_running and highest < 3:
        state = RuntimeQualityState.DATA_STALE
    else:
        state = state_by_layer[highest]
    return RuntimeCredibilityAssessment(state, highest, highest == 7, tuple(layers))
