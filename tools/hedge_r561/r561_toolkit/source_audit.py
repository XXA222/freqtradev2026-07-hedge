from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re

from freqtrade.hedge.r561.environment import audit_powershell_source


REQUIRED = (
    "Dockerfile",
    ".dockerignore",
    "docker/entrypoint-hedge-r561.sh",
    "docker/docker-compose-hedge-r561.yml",
    "scripts/Build-Freqtrade-Hedge-R5.6.1-Docker.ps1",
    "scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Preflight.ps1",
    "scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Commit.ps1",
    "scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Evidence.ps1",
    "scripts/Test-Freqtrade-Hedge-R5.6.1-PowerShell-Ast.ps1",
    "freqtrade/hedge/config_schema_extension.py",
    "freqtrade/hedge/r561/bootstrap.py",
    "freqtrade/hedge/r561/config.py",
    "freqtrade/hedge/r561/docker_state.py",
    "freqtrade/hedge/r561/environment.py",
    "freqtrade/hedge/r561/network.py",
    "freqtrade/hedge/r561/permissions.py",
    "freqtrade/hedge/r561/quality.py",
    "freqtrade/hedge/r561/scenario.py",
    "tools/hedge_r561/r561_toolkit/full_runtime_acceptance.py",
    "freqtrade/hedge/r561/cli.py",
    "config_examples/config_hedge_r55_dryrun_ui.template.json",
    "config_examples/config_hedge_r561_docker_dryrun.template.json",
)


def run(root: Path) -> dict[str, object]:
    checks: dict[str, bool] = {}
    missing = [rel for rel in REQUIRED if not (root / rel).is_file()]
    checks["required_files"] = not missing
    syntax_errors: list[str] = []
    for rel in REQUIRED:
        path = root / rel
        if path.suffix == ".py" and path.is_file():
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except SyntaxError as exc:
                syntax_errors.append(f"{rel}:{exc}")
    checks["python_ast"] = not syntax_errors
    ps_errors: list[str] = []
    ps_warnings: list[str] = []
    for path in (root / "scripts").glob("*R5.6.1*Docker*.ps1"):
        report = audit_powershell_source(path.read_text(encoding="utf-8"))
        ps_errors.extend(f"{path.name}:{item}" for item in report.errors)
        ps_warnings.extend(f"{path.name}:{item}" for item in report.warnings)
    checks["powershell_source_gate"] = not ps_errors
    schema_extension = (root / "freqtrade/hedge/config_schema_extension.py").read_text(
        encoding="utf-8"
    )
    checks["runtime_schema_extensions"] = all(
        marker in schema_extension
        for marker in ('"r56"', '"r561"', '"position_adjustment_owner"')
    )
    ps_ast_source = (
        root / "scripts/Test-Freqtrade-Hedge-R5.6.1-PowerShell-Ast.ps1"
    ).read_text(encoding="utf-8-sig")
    checks["powershell_51_collection_safe"] = (
        "System.Collections.Generic.List" not in ps_ast_source
        and "$ParseErrorRows = @()" in ps_ast_source
        and "errors = $ParseErrorRows" in ps_ast_source
    )
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    checks["python_312_only"] = "python:3.12" in dockerfile and "python:3.14" not in dockerfile
    checks["pip_install_with_dependencies"] = "pip install --no-cache-dir ." in dockerfile and "--no-deps" not in dockerfile
    checks["pip_check"] = "pip check" in dockerfile
    missing_copy_sources: list[str] = []
    import shlex
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("COPY ") or line.startswith("COPY --from="):
            continue
        parts = shlex.split(line)
        sources = [item for item in parts[1:-1] if not item.startswith("--")]
        for source in sources:
            if "*" in source or "?" in source:
                if not list(root.glob(source)):
                    missing_copy_sources.append(source)
            elif not (root / source).exists():
                missing_copy_sources.append(source)
    checks["docker_copy_sources_exist"] = not missing_copy_sources
    checks["docker_context_excludes_runtime"] = all(
        item in (root / ".dockerignore").read_text(encoding="utf-8")
        for item in ("freqtrade-hedge/", "build/", "artifacts/", "user_data/")
    )
    checks["commit_no_pause"] = "--pause=false" in (
        root / "scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Commit.ps1"
    ).read_text(encoding="utf-8")
    compose_text = (root / "docker/docker-compose-hedge-r561.yml").read_text(
        encoding="utf-8"
    )
    entrypoint_text = (root / "docker/entrypoint-hedge-r561.sh").read_text(
        encoding="utf-8"
    )
    checks["docker_api_loopback_publish"] = '"127.0.0.1:8080:8080"' in compose_text
    checks["docker_config_bootstrap"] = (
        "bootstrap-config" in entrypoint_text
        and "config_hedge_r561_docker_dryrun.template.json" in compose_text
    )
    checks["actual_module_cli_invocation"] = all(
        "-m freqtrade.hedge.r561.cli" not in path.read_text(encoding="utf-8-sig")
        for path in (
            root / "scripts/Run-Freqtrade-Hedge-R5.5-DryRun-UI.ps1",
            root / "scripts/Run-Freqtrade-Hedge-R5.6-DryRun-Operations.ps1",
            root / "scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Preflight.ps1",
        )
    )
    config = json.loads(
        (root / "config_examples/config_hedge_r55_dryrun_ui.template.json").read_text(
            encoding="utf-8"
        )
    )
    checks["standard_config_fields"] = all(
        key in config
        for key in (
            "telegram",
            "entry_pricing",
            "exit_pricing",
            "cancel_open_orders_on_exit",
            "order_types",
            "order_time_in_force",
            "unfilledtimeout",
        )
    )
    checks["proxy_trust_env"] = (
        config["exchange"]["ccxt_config"].get("requests_trust_env") is True
        and config["exchange"]["ccxt_async_config"].get("aiohttp_trust_env") is True
    )
    checks["risk_authority"] = all(
        key in config["hedge"]
        for key in (
            "max_gross_notional",
            "max_gross_exposure_ratio",
            "max_single_order_notional",
            "max_margin_utilization",
            "min_liquidation_buffer_ratio",
        )
    )
    checks["write_lock"] = (
        config["dry_run"] is True
        and config["hedge"]["read_only"] is True
        and config["hedge"]["live_trading_enabled"] is False
        and not config["exchange"]["key"]
        and not config["exchange"]["secret"]
    )
    forbidden: list[str] = []
    pattern = re.compile(
        r"(?i)(api[_-]?secret|private[_-]?key|password)\s*=\s*[\"'][^\"']+[\"']"
    )
    for base in (root / "freqtrade/hedge/r561", root / "scripts"):
        for path in base.glob("*R5.6.1*" if base.name == "scripts" else "*.py"):
            if path.is_file() and pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                forbidden.append(str(path.relative_to(root)))
    checks["no_embedded_secrets"] = not forbidden
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "missing": missing,
        "syntax_errors": syntax_errors,
        "powershell_errors": ps_errors,
        "powershell_warnings": ps_warnings,
        "missing_copy_sources": missing_copy_sources,
        "forbidden": forbidden,
        "failed": failed,
        "mainnet_writes": "LOCKED",
        "exchange_access": "NONE",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = run(Path(args.project_root).resolve())
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
