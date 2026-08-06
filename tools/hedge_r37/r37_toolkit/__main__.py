from __future__ import annotations

import argparse
import json
from pathlib import Path

from .acceptance import build_acceptance_report


def main() -> int:
    parser = argparse.ArgumentParser(prog="r37_toolkit")
    parser.add_argument("--report", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--r362-report")
    args = parser.parse_args()
    payload = build_acceptance_report(
        output=Path(args.report),
        source_root=Path(args.source_root),
        installer=Path(args.installer),
        r362_report=None if args.r362_report is None else Path(args.r362_report),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("status") != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
