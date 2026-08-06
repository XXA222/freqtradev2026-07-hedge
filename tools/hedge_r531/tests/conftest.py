from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

if importlib.util.find_spec("ccxt") is None:
    root = Path(__file__).resolve().parents[3]
    package = types.ModuleType("freqtrade.persistence")
    package.__path__ = [str(root / "freqtrade" / "persistence")]
    sys.modules["freqtrade.persistence"] = package
