from .config import load_runtime_config, runtime_config_from_mapping
from .models import (
    ComponentHealth,
    ComponentState,
    DiagnosticResult,
    DiagnosticSummary,
    R4CycleReport,
    R4Mode,
    R4RuntimeConfig,
    RuntimePhase,
    RuntimeReadiness,
)
from .readiness import ReadinessAggregator

__version__ = "4.0.1"

__all__ = [
    "ComponentHealth",
    "ComponentState",
    "DiagnosticResult",
    "DiagnosticSummary",
    "R4CycleReport",
    "R4Mode",
    "R4RuntimeConfig",
    "ReadinessAggregator",
    "RuntimePhase",
    "RuntimeReadiness",
    "load_runtime_config",
    "runtime_config_from_mapping",
]

from .composition import ComponentAdapter, R4CompositionInputs, build_r4_runtime
from .lifecycle import R4RuntimeLifecycle
from .planner import R4HedgePlanner, R4PlanningDecision, R4PlanningInputs
from .runtime import R4ExecutionOutcome, R4ProductionRuntime
from .simulation import R4ScenarioRunner, ScenarioDefinition, ScenarioOutcome

__all__ += [
    "ComponentAdapter", "R4CompositionInputs", "R4HedgePlanner",
    "R4PlanningDecision", "R4PlanningInputs", "R4ExecutionOutcome", "R4ProductionRuntime",
    "R4RuntimeLifecycle", "R4ScenarioRunner", "ScenarioDefinition",
    "ScenarioOutcome", "build_r4_runtime",
]
from .parity import PlannerParityVerifier, planning_fingerprint
from .status import RuntimeStatusPublisher
from .testnet_matrix import R4TestnetMatrixRunner, TestnetMatrixReport

__all__ += [
    "PlannerParityVerifier", "R4TestnetMatrixRunner", "RuntimeStatusPublisher",
    "TestnetMatrixReport", "planning_fingerprint",
]
