from __future__ import annotations

import argparse
import json
from pathlib import Path

from .acceptance import build_acceptance_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--r331-report")
    args = parser.parse_args()
    payload = build_acceptance_report(
        output=Path(args.report),
        r331_report=(None if args.r331_report is None else Path(args.r331_report)),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
