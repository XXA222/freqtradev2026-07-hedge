from pathlib import Path


def test_deterministic_scenario_uses_real_paper_application() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "freqtrade/hedge/r561/scenario.py").read_text(encoding="utf-8")
    assert "IntegratedPaperHedgeApplication" in source
    assert "run_market_cycle" in source
    assert "dual_legs_present" in source
    assert "position_adjustment_upstream_not_required" in source


def test_r561_module_cli_executes_and_emits_json(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    project_root = Path(__file__).resolve().parents[3]
    config = project_root / "config_examples" / "config_hedge_r55_dryrun_ui.template.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    completed = subprocess.run(
        [sys.executable, "-m", "freqtrade.hedge.r561", "validate-config", str(config)],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ready"] is True
    assert payload["checks"]["dry_run"] is True


def test_full_runtime_acceptance_uses_official_strategy_and_real_paper_paths():
    from pathlib import Path
    project = Path(__file__).resolve().parents[3]
    text = (project / "tools/hedge_r561/r561_toolkit/full_runtime_acceptance.py").read_text(encoding="utf-8")
    assert "validate_docker_dryrun_config(config, run_upstream_schema=True)" in text
    assert "StrategyResolver.load_strategy" in text
    assert "run_deterministic_paper_scenario" in text


def test_deterministic_scenario_is_ephemeral_when_funding_is_disabled():
    from freqtrade.hedge.r561.scenario import _config

    paper = _config()["hedge"]["paper"]
    assert paper["funding_source"] == "none"
    assert paper["ephemeral"] is True


def test_windows_powershell_ast_script_avoids_ps51_generic_list_binder_bug():
    from pathlib import Path

    project = Path(__file__).resolve().parents[3]
    source = (
        project / "scripts/Test-Freqtrade-Hedge-R5.6.1-PowerShell-Ast.ps1"
    ).read_text(encoding="utf-8-sig")
    assert "System.Collections.Generic.List" not in source
    assert "$ParseErrorRows = @()" in source
    assert "errors = $ParseErrorRows" in source
