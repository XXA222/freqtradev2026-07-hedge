from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from .acceptance import build_acceptance_report
from .online import run_online


def main() -> int:
    parser = argparse.ArgumentParser(prog="r36_toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    acceptance = sub.add_parser("acceptance")
    acceptance.add_argument("--report", required=True)
    acceptance.add_argument("--r35-report")

    run = sub.add_parser("run")
    run.add_argument("--credential-file", required=True)
    run.add_argument("--journal", required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--mode", choices=("validation-only", "submit-cancel"), default="validation-only")
    run.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), default="ETHUSDT")
    run.add_argument("--position-side", choices=("LONG", "SHORT"), default="LONG")
    run.add_argument("--target-notional", type=Decimal, default=Decimal("10"))
    run.add_argument("--max-order-notional", type=Decimal, default=Decimal("25"))
    run.add_argument("--passive-offset-bps", type=int, default=1000)
    run.add_argument("--proxy")
    run.add_argument("--ready-timeout-seconds", type=float, default=120.0)
    run.add_argument("--arm-ttl-seconds", type=int, default=300)
    run.add_argument("--run-id")
    run.add_argument("--actor", default="local-operator")
    run.add_argument("--confirmed-testnet", action="store_true")
    run.add_argument("--confirmed-submit-cancel", action="store_true")

    args = parser.parse_args()
    if args.command == "acceptance":
        payload = build_acceptance_report(
            output=Path(args.report),
            r35_report=None if args.r35_report is None else Path(args.r35_report),
        )
    else:
        payload = run_online(
            credential_file=Path(args.credential_file),
            journal_file=Path(args.journal),
            report_file=Path(args.report),
            mode=args.mode,
            symbol=args.symbol,
            position_side=args.position_side,
            target_notional=args.target_notional,
            max_order_notional=args.max_order_notional,
            passive_offset_bps=args.passive_offset_bps,
            proxy_url=args.proxy,
            ready_timeout_seconds=args.ready_timeout_seconds,
            arm_ttl_seconds=args.arm_ttl_seconds,
            run_id=args.run_id,
            actor=args.actor,
            confirmed_testnet=args.confirmed_testnet,
            confirmed_submit_cancel=args.confirmed_submit_cancel,
        )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
