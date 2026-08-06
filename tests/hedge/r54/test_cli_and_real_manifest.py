from __future__ import annotations

import json

from freqtrade.hedge.r54.cli import main
from freqtrade.hedge.r54.config_validation import validate_config_manifest


def test_cli_init_and_evaluate_pending(tmp_path, capsys) -> None:
    database = tmp_path / "r54.sqlite"
    assert main(["init-store", "--database", str(database)]) == 0
    assert main(
        [
            "evaluate-stage",
            "--database",
            str(database),
            "--account-id",
            "acct",
            "--stage",
            "MAINNET_READONLY",
        ]
    ) == 2
    output = capsys.readouterr().out.strip().splitlines()
    assert json.loads(output[-1])["status"] == "PENDING_EXTERNAL_EVIDENCE"


def test_r54_config_manifest_is_valid() -> None:
    project_root = __import__("pathlib").Path(__file__).resolve().parents[3]
    manifest = project_root / "config_examples/R5.4-CONFIG-MANIFEST.json"
    passed, details = validate_config_manifest(manifest, root=manifest.parent)
    assert passed, details
