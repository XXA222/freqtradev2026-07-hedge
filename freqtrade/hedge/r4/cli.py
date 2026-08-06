from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_runtime_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freqtrade-hedge-r4")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-config")
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument("--project-root", type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate-config":
        config = load_runtime_config(args.config, project_root=args.project_root)
        print(
            json.dumps(
                {
                    "mode": config.mode.value,
                    "account_id": config.account_id,
                    "symbols": config.symbols,
                    "state_dir": str(config.resolved_state_dir()),
                    "mainnet_live_exchange_write": "LOCKED",
                    "market_orders": "DISABLED",
                },
                sort_keys=True,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
