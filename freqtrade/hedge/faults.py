"""Compatibility imports for the canonical exception hierarchy.

New code should import from :mod:`freqtrade.hedge.errors`.
"""

from freqtrade.hedge.errors import (
    HedgeConfigurationError,
    HedgeContractViolation,
    HedgeDefinitiveError,
    HedgeError,
    HedgePersistenceError,
    HedgeProjectionError,
    HedgeRetryableError,
)

__all__ = [
    "HedgeConfigurationError",
    "HedgeContractViolation",
    "HedgeDefinitiveError",
    "HedgeError",
    "HedgePersistenceError",
    "HedgeProjectionError",
    "HedgeRetryableError",
]
