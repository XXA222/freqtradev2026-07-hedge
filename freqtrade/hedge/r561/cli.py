"""R5.6.1 Docker runtime hardening CLI."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any

from .bootstrap import bootstrap_docker_config
from .config import validate_docker_dryrun_config
from .environment import proxy_environment, validate_python_environment
from .evidence import create_evidence_bundle
from .network import run_public_network_probe
from .permissions import probe_directory
from .scenario import run_deterministic_paper_scenario


def _load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _validate(args: argparse.Namespace) -> int:
    config = _load(args.config)
    report = validate_docker_dryrun_config(config, run_upstream_schema=args.upstream_schema)
    print(_dump(asdict(report)))
    return 0 if report.ready else 2


def _network(args: argparse.Namespace) -> int:
    config = _load(args.config)
    result = run_public_network_probe(config, symbol=args.symbol, attempts=args.attempts)
    payload = {name: asdict(report) for name, report in result.items()}
    print(_dump(payload))
    return 0 if all(report.ready for report in result.values()) else 2


def _scenario(_: argparse.Namespace) -> int:
    report = run_deterministic_paper_scenario()
    print(_dump(asdict(report)))
    return 0 if report.ready else 2


def _preflight(args: argparse.Namespace) -> int:
    config = _load(args.config)
    config_report = validate_docker_dryrun_config(config, run_upstream_schema=args.upstream_schema)
    permission = probe_directory(args.user_data)
    try:
        import freqtrade

        import_root = str(Path(freqtrade.__file__).resolve())
    except Exception:
        import_root = None
    python_report = validate_python_environment(
        required_major_minor=(3, 12),
        import_root=import_root,
        expected_import_root=args.project_root,
    )
    result = {
        "config": asdict(config_report),
        "permission": asdict(permission),
        "python": asdict(python_report),
        "proxy_environment": proxy_environment(os.environ),
        "observed_at": datetime.now(UTC).isoformat(),
        "mainnet_writes": "LOCKED",
    }
    print(_dump(result))
    return 0 if config_report.ready and permission.ready and python_report.ready else 2



def _bootstrap(args: argparse.Namespace) -> int:
    report = bootstrap_docker_config(template_path=args.template, destination=args.destination)
    print(_dump(asdict(report)))
    return 0 if report.ready else 2


def _bundle(args: argparse.Namespace) -> int:
    evidence = json.loads(Path(args.evidence_json).read_text(encoding="utf-8"))
    result = create_evidence_bundle(destination=args.output, evidence=evidence)
    print(str(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freqtrade-hedge-r561")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-config")
    validate.add_argument("config")
    validate.add_argument("--upstream-schema", action="store_true")
    validate.set_defaults(handler=_validate)

    network = sub.add_parser("network-probe")
    network.add_argument("config")
    network.add_argument("--symbol", default="BTC/USDT:USDT")
    network.add_argument("--attempts", type=int, default=20)
    network.set_defaults(handler=_network)

    scenario = sub.add_parser("deterministic-scenario")
    scenario.set_defaults(handler=_scenario)

    preflight = sub.add_parser("preflight")
    preflight.add_argument("config")
    preflight.add_argument("--project-root", required=True)
    preflight.add_argument("--user-data", required=True)
    preflight.add_argument("--upstream-schema", action="store_true")
    preflight.set_defaults(handler=_preflight)

    bootstrap = sub.add_parser("bootstrap-config")
    bootstrap.add_argument("--template", required=True)
    bootstrap.add_argument("--destination", required=True)
    bootstrap.set_defaults(handler=_bootstrap)

    bundle = sub.add_parser("evidence-bundle")
    bundle.add_argument("evidence_json")
    bundle.add_argument("--output", required=True)
    bundle.set_defaults(handler=_bundle)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
