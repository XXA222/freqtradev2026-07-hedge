from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .baseline import create_baseline
from .readonly import create_overlay, create_safe_base_config, run_preflight


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freqtrade-hedge-r23")
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze", help="Create a secret-safe frozen source baseline")
    freeze.add_argument("--project-root", type=Path, required=True)
    freeze.add_argument("--output-root", type=Path)
    freeze.add_argument("--label", default="R2.2_FULL_PASS")
    freeze.add_argument("--tag", default="hedge-r2.2-full-pass")
    freeze.add_argument("--require-clean", action="store_true")

    overlay = subparsers.add_parser("overlay", help="Create the R2.3 readonly proxy overlay")
    overlay.add_argument("--proxy", required=True)
    overlay.add_argument("--output", type=Path, required=True)
    overlay.add_argument("--db-url")

    safe_base = subparsers.add_parser("safe-base", help="Create a minimal safe Binance futures config")
    safe_base.add_argument("--output", type=Path, required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="Run futures-required REST/CCXT/WebSocket checks and optional Spot diagnostics",
    )
    preflight.add_argument("--proxy", required=True)
    preflight.add_argument("--report", type=Path, required=True)
    preflight.add_argument("--pair", default="BTC/USDT:USDT")
    preflight.add_argument("--timeout", type=float, default=25.0)
    preflight.add_argument("--skip-websocket", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "freeze":
            result = create_baseline(
                args.project_root,
                output_root=args.output_root,
                label=args.label,
                tag=args.tag,
                require_clean=args.require_clean,
            )
        elif args.command == "overlay":
            result = create_overlay(args.proxy, args.output, db_url=args.db_url)
        elif args.command == "safe-base":
            result = create_safe_base_config(args.output)
        else:
            result = run_preflight(
                args.proxy,
                report_path=args.report,
                pair=args.pair,
                timeout=args.timeout,
                require_ws=not args.skip_websocket,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.command == "preflight" and result["status"] != "PASS":
            return 2
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI error boundary
        print(
            json.dumps(
                {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
