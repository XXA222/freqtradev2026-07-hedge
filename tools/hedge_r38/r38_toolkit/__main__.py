from __future__ import annotations

import argparse
import json
from pathlib import Path

from .acceptance import build_acceptance_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--r37-report", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    payload = build_acceptance_report(
        output=args.report,
        r37_report=args.r37_report,
        project_root=args.project_root,
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if not payload["required_failures"] else 2


raise SystemExit(main())
