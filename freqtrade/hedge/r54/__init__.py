"""R5.4 operational evidence and staged-promotion toolkit.

R5.4 does not unlock exchange writes by itself.  It turns the remaining external
operational work (soak observations, PostgreSQL drills, Testnet/Mainnet canaries,
backup/restore, monitoring and promotion) into durable, hash-bound evidence.
"""

from .models import (
    ArtifactKind,
    CheckResult,
    CheckStatus,
    EvidenceEnvelope,
    GateDecision,
    GateStatus,
)
from .readiness import R54ReadinessEvaluator
from .runner import CommandSpec, ContinueAllSummary, run_continue_all
from .store import R54OperationalStore

__all__ = [
    "ArtifactKind",
    "CheckResult",
    "CheckStatus",
    "EvidenceEnvelope",
    "GateDecision",
    "GateStatus",
    "R54OperationalStore",
    "CommandSpec",
    "ContinueAllSummary",
    "run_continue_all",
    "R54ReadinessEvaluator",
]
