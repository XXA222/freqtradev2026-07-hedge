from __future__ import annotations

import argparse
import json
from pathlib import Path

from .acceptance import build_acceptance_report, write_report_atomic


def main() -> int:
    parser = argparse.ArgumentParser(description="R3.2 main-program integration acceptance")
    parser.add_argument("--r31-report", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    report_path = Path(args.report)
    payload = build_acceptance_report(
        r31_report=args.r31_report,
        project_root=args.project_root,
    )
    digest = write_report_atomic(payload, report_path)
    payload = dict(payload)
    payload["report_sha256"] = digest
    write_report_atomic(payload, report_path)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if payload["status"] == "MAIN_PROGRAM_INTEGRATION_COMPLETE_LIVE_LOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
