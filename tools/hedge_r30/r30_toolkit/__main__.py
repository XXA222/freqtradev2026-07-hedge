from __future__ import annotations

import argparse
import json
from pathlib import Path

from .readiness import build_readiness_report, write_report_atomic


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the R3.0 production execution gate")
    parser.add_argument("--r24-report", required=True)
    parser.add_argument("--r25-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-order-notional", default="100")
    args = parser.parse_args()
    report = build_readiness_report(
        r24_report=args.r24_report,
        r25_report=args.r25_report,
        max_order_notional=args.max_order_notional,
    )
    digest = write_report_atomic(report, Path(args.output))
    report["report_sha256"] = digest
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
