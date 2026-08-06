from __future__ import annotations

import argparse
import json
from pathlib import Path

from freqtrade.hedge.r5.readonly_bootstrap import prepare_readonly_workspace

from .acceptance import build_acceptance_report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r51")
    sub = value.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--project-root", required=True)
    prepare.add_argument("--legacy-credentials", required=True)
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--allow-no-ip-restriction", action="store_true")
    prepare.add_argument("--allow-no-futures-permission", action="store_true")
    final = sub.add_parser("finalize")
    final.add_argument("--output", required=True)
    final.add_argument("--stage-report", required=True)
    final.add_argument("--stream-report", required=True)
    final.add_argument("--workspace-manifest", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "prepare":
        workspace = prepare_readonly_workspace(
            project_root=args.project_root,
            legacy_credentials=args.legacy_credentials,
            output_dir=args.output_dir,
            require_ip_restriction=not args.allow_no_ip_restriction,
            require_futures_permission=not args.allow_no_futures_permission,
        )
        payload = workspace.to_dict()
        manifest = Path(args.manifest).expanduser().resolve(strict=False)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="ascii")
        print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        return 0
    payload = build_acceptance_report(
        output=args.output,
        stage_report=args.stage_report,
        stream_report=args.stream_report,
        workspace_manifest=args.workspace_manifest,
    )
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if payload["mainnet_readonly_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
