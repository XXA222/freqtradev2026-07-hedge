from __future__ import annotations

import argparse
import json
from pathlib import Path

from .acceptance import build_acceptance_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--r4-report", type=Path)
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args()
    payload = build_acceptance_report(
        output=args.output,
        r4_report=args.r4_report,
        project_root=args.project_root,
    )
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if payload["r5_limited_mainnet_core_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
