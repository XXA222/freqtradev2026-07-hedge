from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from .acceptance import build_acceptance_report
from .canary import run


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    acceptance = sub.add_parser("acceptance")
    acceptance.add_argument("--report", required=True)
    acceptance.add_argument("--r34-report")

    canary = sub.add_parser("canary")
    canary.add_argument("--credential-file", required=True)
    canary.add_argument("--report", required=True)
    canary.add_argument("--mode", choices=("validation-only", "submit-cancel"), default="validation-only")
    canary.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    canary.add_argument("--position-side", choices=("LONG", "SHORT"), required=True)
    canary.add_argument("--quantity", type=Decimal, required=True)
    canary.add_argument("--limit-price", type=Decimal, required=True)
    canary.add_argument("--max-notional", type=Decimal, default=Decimal("25"))
    canary.add_argument("--proxy")
    canary.add_argument("--ready-timeout-seconds", type=float, default=120.0)
    canary.add_argument("--confirmed-testnet", action="store_true")
    canary.add_argument("--confirmed-submit-cancel", action="store_true")

    args = parser.parse_args()
    if args.command == "acceptance":
        payload = build_acceptance_report(
            output=Path(args.report),
            r34_report=None if args.r34_report is None else Path(args.r34_report),
        )
    else:
        payload = run(
            credential_file=Path(args.credential_file),
            report_file=Path(args.report),
            mode=args.mode,
            symbol=args.symbol,
            position_side=args.position_side,
            quantity=args.quantity,
            limit_price=args.limit_price,
            max_notional=args.max_notional,
            proxy_url=args.proxy,
            ready_timeout_seconds=args.ready_timeout_seconds,
            confirmed_testnet=args.confirmed_testnet,
            confirmed_submit_cancel=args.confirmed_submit_cancel,
        )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
