from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Callable

from freqtrade.hedge.execution.service import PositionSide

from .budget import R5DailyBudgetStore
from .config import load_config
from .credentials import load_mainnet_credentials
from .evidence import load_evidence_bundle
from .executor import R5OrderExecutor
from .journal import R5Journal
from .mainnet_api import R5MainnetReadApi
from .models import R5Stage
from .orchestrator import R5MainnetOrchestrator, R5RunRequest
from .promotion import load_promotion_manifest
from .strategy_bridge import R5RestrictedAutoBridge


def _secret_file(path: str | None, name: str, *, required: bool) -> str | None:
    if path is None:
        if required:
            raise ValueError(f"{name} file is required")
        return None
    resolved = Path(path).expanduser().resolve(strict=True)
    lines = resolved.read_text(encoding="utf-8-sig").splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise ValueError(f"{name} file must contain exactly one non-empty line")
    return lines[0].strip()


def _load_strategy(spec: str):
    if ":" not in spec:
        raise ValueError("auto bridge must use module:callable syntax")
    module_name, attr = spec.split(":", 1)
    value = getattr(importlib.import_module(module_name), attr)
    if not callable(value):
        raise TypeError("auto bridge target must be callable")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freqtrade-hedge-r5")
    parser.add_argument("--config", required=True)
    parser.add_argument("--project-root")
    parser.add_argument("--stage", required=True, choices=[stage.value for stage in R5Stage])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--symbol", choices=["BTCUSDT", "ETHUSDT"], default="ETHUSDT")
    parser.add_argument("--position-side", choices=["LONG", "SHORT"], default="LONG")
    parser.add_argument("--target-notional", default="10")
    parser.add_argument("--max-cycles", type=int, default=10)
    parser.add_argument("--operator-token-file")
    parser.add_argument("--risk-token-file")
    parser.add_argument("--confirmation-file")
    parser.add_argument("--promotion-token-file")
    parser.add_argument("--promotion-manifest")
    parser.add_argument("--auto-bridge", help="module:callable returning OrderIntent values")
    parser.add_argument("--proxy-url")
    parser.add_argument("--output", required=True)
    return parser


def _run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve(strict=True)
    stage = R5Stage(args.stage)
    if stage.performs_exchange_write:
        raise PermissionError(
            "LEGACY_R5_WRITE_PATH_DISABLED_USE_R53_AUTHORITATIVE"
        )
    project_root = (
        Path(args.project_root).expanduser().resolve(strict=True)
        if args.project_root
        else config_path.parent
    )
    config = load_config(
        config_path,
        project_root=project_root,
        allow_unarmed_readonly=stage is R5Stage.MAINNET_READONLY,
    )
    credentials = load_mainnet_credentials(
        config.credentials_path,
        allow_legacy_readonly=stage is R5Stage.MAINNET_READONLY,
    )
    operator = _secret_file(args.operator_token_file, "operator token", required=stage.performs_exchange_write)
    risk = _secret_file(args.risk_token_file, "risk token", required=stage.can_create_position)
    confirmation = _secret_file(args.confirmation_file, "confirmation", required=stage.performs_exchange_write)
    promotion_token = _secret_file(args.promotion_token_file, "promotion token", required=stage is R5Stage.EXPANDED_LIMITS)
    manifest = None
    if stage is R5Stage.EXPANDED_LIMITS:
        if args.promotion_manifest is None:
            raise ValueError("EXPANDED_LIMITS requires --promotion-manifest")
        manifest = load_promotion_manifest(args.promotion_manifest)
    evidence = load_evidence_bundle(
        r4_report=config.r4_report,
        user_stream_report=config.user_stream_report,
        r24_report=config.r24_report,
        max_age_seconds=config.evidence_max_age_seconds,
    )
    read_api = R5MainnetReadApi(credentials=credentials, proxy_url=args.proxy_url)

    def executor_factory(profile, gate_evidence, token, actor):
        return R5OrderExecutor(
            credentials=credentials,
            profile=profile,
            evidence=gate_evidence,
            operator_token=token,
            actor=actor,
            proxy_url=args.proxy_url,
        )

    auto_factory = None
    if args.auto_bridge:
        strategy = _load_strategy(args.auto_bridge)

        def auto_factory(stage_value, profile, executor, permit):
            bridge = R5RestrictedAutoBridge(
                stage=stage_value,
                profile=profile,
                account_id=credentials.account_id,
                strategy=strategy,
                executor=executor,
                permit=permit,
                budget=budget,
            )
            return bridge.cycle

    output = Path(args.output).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    with R5Journal(config.journal_path) as journal, R5DailyBudgetStore(config.budget_path) as budget:
        orchestrator = R5MainnetOrchestrator(
            config=config,
            credentials=credentials,
            evidence=evidence,
            read_api=read_api,
            journal=journal,
            budget=budget,
            executor_factory=executor_factory,
            auto_cycle_factory=auto_factory,
        )
        request = R5RunRequest(
            stage=stage,
            run_id=args.run_id,
            actor=args.actor,
            symbol=args.symbol,
            position_side=PositionSide(args.position_side),
            target_notional=args.target_notional,
            operator_token=operator,
            risk_token=risk,
            confirmation=confirmation,
            max_cycles=args.max_cycles,
        )
        report = orchestrator.run(
            request,
            promotion_manifest=manifest,
            promotion_token=promotion_token,
        )
    output.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report.get("status") == "PASS" else 2


def _structured_failure(args: argparse.Namespace, exc: Exception) -> int:
    payload = {
        "schema_version": 1,
        "toolkit_version": "5.1.6",
        "stage": str(getattr(args, "stage", "UNKNOWN")),
        "run_id": str(getattr(args, "run_id", "UNKNOWN")),
        "state": "FAILED",
        "status": "FAILED",
        "error": str(exc),
        "exception_type": type(exc).__name__,
        "writes_attempted": 0,
        "real_mainnet_order_requests_sent": 0,
        "market_orders": "DISABLED",
    }
    output_value = getattr(args, "output", None)
    if output_value:
        try:
            output = Path(output_value).expanduser().resolve(strict=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                encoding="ascii",
            )
        except Exception:
            pass
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True), file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:
        return _structured_failure(args, exc)


if __name__ == "__main__":
    raise SystemExit(main())
