from __future__ import annotations

import argparse
import json
from pathlib import Path

from .acceptance import build_acceptance_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--r381-report", type=Path)
    args = parser.parse_args()
    payload = build_acceptance_report(output=args.output, r381_report=args.r381_report)
    print(json.dumps(payload, sort_keys=True, default=str))
    return 0 if payload["r4_full_completion_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
