from pathlib import Path

from tools.hedge_r561.r561_toolkit.offline_acceptance import run as acceptance
from tools.hedge_r561.r561_toolkit.source_audit import run as audit


def test_r561_offline_acceptance() -> None:
    root = Path(__file__).resolve().parents[3]
    result = acceptance(root)
    assert result["status"] == "PASS", result
    assert result["passed"] == result["total"]


def test_r561_source_audit() -> None:
    root = Path(__file__).resolve().parents[3]
    result = audit(root)
    assert result["status"] == "PASS", result
