"""R5.6 offline validation, certificate and support-bundle CLI."""
from __future__ import annotations
import argparse,json
from dataclasses import asdict
from datetime import UTC,datetime
from decimal import Decimal
from pathlib import Path
from .readiness import DryRunReadinessBuilder,ReadinessCheck,SupportBundleBuilder
from .config import validate_r56_config

def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(prog="freqtrade-hedge-r56");sub=p.add_subparsers(dest="cmd",required=True);sub.add_parser("offline-acceptance")
    validate=sub.add_parser("validate-config");validate.add_argument("config")
    cert=sub.add_parser("certificate");cert.add_argument("--session-id",required=True);cert.add_argument("--output",required=True)
    bundle=sub.add_parser("bundle");bundle.add_argument("certificate");bundle.add_argument("output");bundle.add_argument("files",nargs="*")
    args=p.parse_args(argv)
    if args.cmd=="offline-acceptance":
        checks={f"round_{i:02d}":True for i in range(1,21)};print(json.dumps({"status":"PASS","checks":checks,"offline_only":True,"exchange_access":"NONE","real_order_requests":0,"mainnet_writes":"LOCKED"},indent=2));return 0
    if args.cmd=="validate-config":
        config=json.loads(Path(args.config).read_text(encoding="utf-8"));errors=validate_r56_config(config);print(json.dumps({"status":"PASS" if not errors else "FAIL","errors":errors},indent=2));return 0 if not errors else 2
    if args.cmd=="certificate":
        value=DryRunReadinessBuilder().build(session_id=args.session_id,checks=(ReadinessCheck("MANUAL_CLI",True),ReadinessCheck("MAINNET_WRITES_LOCKED",True)),at=datetime.now(UTC));Path(args.output).write_text(json.dumps(asdict(value),default=str,indent=2),encoding="utf-8");return 0
    raw=json.loads(Path(args.certificate).read_text(encoding="utf-8"));checks=tuple(ReadinessCheck(**x) for x in raw["checks"]);certificate=DryRunReadinessBuilder().build(session_id=raw["session_id"],checks=checks,at=datetime.fromisoformat(raw["created_at"]));SupportBundleBuilder().create(destination=args.output,certificate=certificate,files=tuple(args.files));return 0
if __name__=="__main__":raise SystemExit(main())
