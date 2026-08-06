from __future__ import annotations

import json
import os
import sys

from freqtrade.hedge.r54.canonical import sha256_file
from freqtrade.hedge.r54.config_validation import validate_config_manifest
from freqtrade.hedge.r54.runner import CommandSpec, run_continue_all
from freqtrade.hedge.r54.upstream import UpstreamFileExpectation, verify_upstream_files


def test_upstream_verifier_detects_drift(tmp_path) -> None:
    target = tmp_path / "core.py"
    target.write_text("x=1\n", encoding="utf-8")
    expectation = UpstreamFileExpectation("core.py", sha256_file(target))
    assert verify_upstream_files(tmp_path, (expectation,))[0]
    target.write_text("x=2\n", encoding="utf-8")
    assert not verify_upstream_files(tmp_path, (expectation,))[0]


def test_config_manifest_types_and_json(tmp_path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"entries": [{"path": "config.json", "type": "FULL_CONFIG"}]}),
        encoding="utf-8",
    )
    assert validate_config_manifest(manifest, root=tmp_path) == (
        True,
        {"checked": 1, "errors": []},
    )


def test_continue_all_runs_after_failure(tmp_path) -> None:
    specs = (
        CommandSpec("fail", (sys.executable, "-c", "raise SystemExit(3)")),
        CommandSpec("pass", (sys.executable, "-c", "print('after')")),
    )
    summary = run_continue_all(specs, artifact_dir=tmp_path / "artifacts")
    assert not summary.passed
    assert summary.failed_names == ("fail",)
    assert len(summary.results) == 2
    assert summary.results[1].passed
    assert "after" in (tmp_path / "artifacts" / "02_pass.stdout.log").read_text()
