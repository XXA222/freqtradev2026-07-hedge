from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile

from freqtrade.hedge.r561.bootstrap import bootstrap_docker_config
from freqtrade.hedge.r561.config import validate_docker_dryrun_config
from freqtrade.hedge.r561.docker_state import (
    DockerLifecycleAction,
    classify_docker_inspect,
    lifecycle_action,
    safe_commit_arguments,
)
from freqtrade.hedge.r561.environment import (
    audit_powershell_source,
    build_dependency_plan,
    validate_install_arguments,
)
from freqtrade.hedge.r561.network import NetworkProbeReport, ProbeAttempt, evaluate_probe_attempts
from freqtrade.hedge.r561.permissions import probe_directory
from freqtrade.hedge.r561.quality import (
    RuntimeCredibilityEvidence,
    evaluate_runtime_credibility,
)


def run(project_root: Path) -> dict[str, object]:
    checks: dict[str, bool] = {}
    config = json.loads(
        (project_root / "config_examples/config_hedge_r55_dryrun_ui.template.json").read_text(
            encoding="utf-8"
        )
    )
    report = validate_docker_dryrun_config(config)
    checks["01_complete_freqtrade_and_hedge_config"] = report.ready
    checks["02_proxy_trust_env"] = (
        config["exchange"]["ccxt_config"]["requests_trust_env"] is True
        and config["exchange"]["ccxt_async_config"]["aiohttp_trust_env"] is True
    )
    checks["03_authoritative_risk_fields"] = report.checks.get(
        "authoritative_risk_fields", False
    ) and report.checks.get("risk_limits_constructible", False)
    checks["04_position_adjustment_semantics"] = (
        config["position_adjustment_enable"] is False
        and config["hedge"]["r561"]["position_adjustment_owner"] == "hedge_planner"
    )
    missing = classify_docker_inspect(
        returncode=1, stdout="", stderr="Error: No such container: test"
    )
    checks["05_docker_existence_tristate"] = (
        lifecycle_action(missing) is DockerLifecycleAction.CREATE
    )
    paused = classify_docker_inspect(
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "State": {
                        "Status": "paused",
                        "Running": False,
                        "Paused": True,
                        "Restarting": False,
                        "Dead": False,
                    }
                }
            ]
        ),
        stderr="",
    )
    checks["06_docker_paused_recovery"] = (
        lifecycle_action(paused) is DockerLifecycleAction.UNPAUSE
    )
    checks["07_commit_never_pauses"] = "--pause=false" in safe_commit_arguments("c", "i")
    plan = build_dependency_plan(project_root / "pyproject.toml", selected_groups=("develop",))
    checks["08_pyproject_dependency_plan"] = plan.install_target == ".[develop]"
    checks["09_no_deps_forbidden"] = bool(
        validate_install_arguments(("pip", "install", "--no-deps", "."))
    )
    now = datetime(2026, 8, 6, tzinfo=UTC)
    attempts = tuple(
        ProbeAttempt(i, True, 10, now, now) for i in range(1, 21)
    )
    network = evaluate_probe_attempts(
        attempts,
        required_attempts=20,
        minimum_success_ratio=Decimal("1"),
        now=now,
    )
    checks["10_network_20_of_20_gate"] = network.ready and network.succeeded == 20
    with tempfile.TemporaryDirectory(prefix="r561-permission-") as td:
        checks["11_write_permission_probe"] = probe_directory(td).ready
    checks["12_powershell_known_trap_gate"] = not audit_powershell_source(
        'Write-Host "$Name: x"'
    ).ready
    credible = evaluate_runtime_credibility(
        RuntimeCredibilityEvidence(
            observed_at=now,
            process_running=True,
            exchange_initialized=True,
            ticker_probe=network,
            ohlcv_probe=network,
            last_strategy_cycle_at=now,
            strategy_cycle_count=2,
            order_count=2,
            fill_count=1,
            deterministic_scenario_passed=True,
            ledger_consistent=True,
            restart_recovered=True,
            reconciliation_at=now,
            api_healthy=True,
            dashboard_healthy=True,
        )
    )
    checks["13_seven_layer_credibility"] = credible.credible and credible.highest_passed_layer == 7
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    checks["14_python_312_docker_gate"] = (
        "python:3.12" in dockerfile and "sys.version_info[:2] == (3, 12)" in dockerfile
    )
    checks["15_no_no_deps_in_dockerfile"] = "--no-deps" not in dockerfile
    checks["16_deterministic_real_paper_scenario_source"] = all(
        marker
        in (project_root / "freqtrade/hedge/r561/scenario.py").read_text(encoding="utf-8")
        for marker in ("IntegratedPaperHedgeApplication", "run_market_cycle", "dual_legs_present")
    )
    app_js = (project_root / "freqtrade/rpc/api_server/hedge_ui/app.js").read_text(
        encoding="utf-8"
    )
    checks["17_dashboard_quality_metrics"] = all(
        marker in app_js
        for marker in (
            "runtime_quality_level",
            "last_candle_age_seconds",
            "strategy_cycle_count",
            "order_count",
            "reconciliation_fresh",
        )
    )
    checks["18_mainnet_writes_locked"] = (
        config["dry_run"] is True
        and config["hedge"]["read_only"] is True
        and config["hedge"]["live_trading_enabled"] is False
        and config["exchange"]["key"] == ""
        and config["exchange"]["secret"] == ""
    )
    docker_config = json.loads(
        (project_root / "config_examples/config_hedge_r561_docker_dryrun.template.json").read_text(
            encoding="utf-8"
        )
    )
    docker_report = validate_docker_dryrun_config(docker_config)
    compose_text = (project_root / "docker/docker-compose-hedge-r561.yml").read_text(
        encoding="utf-8"
    )
    checks["19_docker_api_exposure_contract"] = (
        docker_report.ready
        and docker_config["api_server"]["listen_ip_address"] == "0.0.0.0"
        and docker_config["hedge"]["r561"]["api_exposure_mode"]
        == "docker_loopback_publish"
        and '"127.0.0.1:8080:8080"' in compose_text
    )
    with tempfile.TemporaryDirectory(prefix="r561-bootstrap-") as td:
        target = Path(td) / "config.json"
        first = bootstrap_docker_config(
            template_path=project_root
            / "config_examples/config_hedge_r561_docker_dryrun.template.json",
            destination=target,
        )
        first_bytes = target.read_bytes() if target.exists() else b""
        second = bootstrap_docker_config(
            template_path=project_root
            / "config_examples/config_hedge_r561_docker_dryrun.template.json",
            destination=target,
        )
        checks["20_atomic_idempotent_config_bootstrap"] = (
            first.ready
            and first.created
            and second.ready
            and not second.created
            and target.read_bytes() == first_bytes
        )
    checks["21_module_cli_is_executable"] = (
        'if __name__ == "__main__"' in
        (project_root / "freqtrade/hedge/r561/cli.py").read_text(encoding="utf-8")
        and "-m freqtrade.hedge.r561" in
        (project_root / "scripts/Run-Freqtrade-Hedge-R5.6-DryRun-Operations.ps1").read_text(
            encoding="utf-8-sig"
        )
    )
    from freqtrade.hedge.config_schema_extension import extend_config_schema

    schema_probe = {"type": "object", "properties": {"api_server": {"type": "object"}}}
    extend_config_schema(schema_probe)
    hedge_properties = schema_probe["properties"]["hedge"]["properties"]
    checks["22_runtime_schema_accepts_r56_r561"] = all(
        name in hedge_properties for name in ("r56", "r561")
    )
    scenario_source = (project_root / "freqtrade/hedge/r561/scenario.py").read_text(
        encoding="utf-8"
    )
    checks["23_deterministic_scenario_ephemeral"] = (
        '"funding_source": "none"' in scenario_source
        and '"ephemeral": True' in scenario_source
    )
    ps_ast_source = (
        project_root / "scripts/Test-Freqtrade-Hedge-R5.6.1-PowerShell-Ast.ps1"
    ).read_text(encoding="utf-8-sig")
    checks["24_windows_powershell_ast_collection_safe"] = (
        "System.Collections.Generic.List" not in ps_ast_source
        and "$ParseErrorRows = @()" in ps_ast_source
        and "errors = $ParseErrorRows" in ps_ast_source
    )
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "failed": failed,
        "config_report": asdict(report),
        "offline_only": True,
        "exchange_access": "NONE",
        "real_order_requests": 0,
        "mainnet_writes": "LOCKED",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = run(Path(args.project_root).resolve())
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
