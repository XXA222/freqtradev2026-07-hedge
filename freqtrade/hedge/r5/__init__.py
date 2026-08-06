"""R5 limited-mainnet production staging and strategy runtime for Freqtrade Hedge."""

from .adoption import (
    R5PositionAdoptionManifest,
    apply_position_adoption_manifest,
    build_position_adoption_manifest,
    verify_position_adoption_manifest,
)
from .fill_processor import R5InventoryEvent, R5InventoryEventType, R5InventoryProcessor
from .models import (
    R5AccountSnapshot,
    R5LimitProfile,
    R5RunState,
    R5Stage,
    R5StageReport,
    default_limit_profile,
)
from .orchestrator import R5MainnetOrchestrator, R5RunRequest
from .position_ledger import R5CoreTacticalLedger
from .strategy_runtime import (
    R5PositionPolicy,
    R5ProductionStrategyRuntime,
    R5StrategyCycleResult,
    R5StrategyFacts,
    R5StrategyMode,
    R5StrategySignals,
)
from .strategy_session import R5StrategySession, R5StrategySessionConfig
from .strategy_shadow import facts_from_preflight
from .strategy_store import R5StrategyStateStore

__all__ = [
    "R5AccountSnapshot",
    "R5CoreTacticalLedger",
    "R5InventoryEvent",
    "R5InventoryEventType",
    "R5InventoryProcessor",
    "R5LimitProfile",
    "R5MainnetOrchestrator",
    "R5PositionAdoptionManifest",
    "R5PositionPolicy",
    "R5ProductionStrategyRuntime",
    "R5RunRequest",
    "R5RunState",
    "R5Stage",
    "R5StageReport",
    "R5StrategyCycleResult",
    "R5StrategyFacts",
    "R5StrategyMode",
    "R5StrategySession",
    "R5StrategySessionConfig",
    "R5StrategySignals",
    "R5StrategyStateStore",
    "apply_position_adoption_manifest",
    "build_position_adoption_manifest",
    "default_limit_profile",
    "facts_from_preflight",
    "verify_position_adoption_manifest",
]

__version__ = "5.2.3"
