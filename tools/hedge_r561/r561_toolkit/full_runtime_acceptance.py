"""Full installed-runtime acceptance without exchange access."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import tempfile
from typing import Any

from freqtrade.hedge.r561.config import validate_docker_dryrun_config
from freqtrade.hedge.r561.scenario import run_deterministic_paper_scenario


def _config(project_root: Path) -> dict[str, Any]:
    payload = json.loads(
        (project_root / "config_examples/config_hedge_r55_dryrun_ui.template.json").read_text(
            encoding="utf-8-sig"
        )
    )
    payload["api_server"]["password"] = secrets.token_urlsafe(24)
    payload["api_server"]["jwt_secret_key"] = secrets.token_urlsafe(48)
    payload["api_server"]["ws_token"] = secrets.token_urlsafe(32)
    payload["strategy_path"] = str((project_root / "config_examples/strategies").resolve())
    return payload


def run(project_root: Path) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    errors: list[str] = []
    config = _config(project_root)
    config_report = validate_docker_dryrun_config(config, run_upstream_schema=True)
    checks["official_config_consistency"] = config_report.ready
    if not config_report.ready:
        errors.extend(config_report.errors)
    strategy_name = ""
    try:
        from freqtrade.enums import RunMode
        from freqtrade.resolvers import StrategyResolver

        with tempfile.TemporaryDirectory(prefix="r561-strategy-") as td:
            strategy_config = deepcopy(config)
            strategy_config["user_data_dir"] = Path(td)
            strategy_config["datadir"] = Path(td) / "data"
            strategy_config["runmode"] = RunMode.DRY_RUN
            strategy = StrategyResolver.load_strategy(strategy_config)
            strategy_name = type(strategy).__name__
            checks["strategy_resolves"] = strategy_name == str(config["strategy"])
    except Exception as exc:
        checks["strategy_resolves"] = False
        errors.append(f"STRATEGY_RESOLUTION_FAILED:{type(exc).__name__}:{exc}")
    try:
        scenario = run_deterministic_paper_scenario()
        checks["deterministic_paper_scenario"] = scenario.ready
        if not scenario.ready:
            errors.extend(scenario.reasons)
        scenario_payload = asdict(scenario)
    except Exception as exc:
        checks["deterministic_paper_scenario"] = False
        errors.append(f"DETERMINISTIC_SCENARIO_FAILED:{type(exc).__name__}:{exc}")
        scenario_payload = None
    checks["mainnet_writes_locked"] = (
        config["dry_run"] is True
        and config["hedge"]["read_only"] is True
        and config["hedge"]["live_trading_enabled"] is False
        and not config["exchange"]["key"]
        and not config["exchange"]["secret"]
    )
    return {
        "status": "PASS" if all(checks.values()) and not errors else "FAIL",
        "checks": checks,
        "errors": errors,
        "strategy": strategy_name,
        "config_report": asdict(config_report),
        "scenario": scenario_payload,
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
