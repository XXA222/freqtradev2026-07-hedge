from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# The delivery build container intentionally lacks optional Freqtrade exchange/UI
# dependencies.  Isolate hedge persistence metadata when those packages are absent;
# a normal installed Freqtrade environment uses the real package unchanged.
if importlib.util.find_spec("ccxt") is None:
    root = Path(__file__).resolve().parents[3]
    package = types.ModuleType("freqtrade.persistence")
    package.__path__ = [str(root / "freqtrade" / "persistence")]
    sys.modules["freqtrade.persistence"] = package
