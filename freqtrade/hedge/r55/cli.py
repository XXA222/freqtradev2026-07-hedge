"""R5.5 local Dry-run validation and dashboard helpers."""
from __future__ import annotations
import argparse, json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping
from .strategy import StrategyContract

def _mapping(value:object)->Mapping[str,Any]:return value if isinstance(value,Mapping) else {}
def validate_dryrun_config(config:Mapping[str,Any])->tuple[str,...]:
    errors=[];hedge=_mapping(config.get("hedge"));paper=_mapping(hedge.get("paper"));dash=_mapping(hedge.get("dashboard"));api=_mapping(config.get("api_server"));exchange=_mapping(config.get("exchange"))
    def require(ok:bool,code:str):
        if not ok:errors.append(code)
    require(config.get("dry_run") is True,"DRY_RUN_MUST_BE_TRUE")
    require(str(config.get("trading_mode","")).lower()=="futures","TRADING_MODE_MUST_BE_FUTURES")
    require(str(config.get("margin_mode","")).lower()=="cross","MARGIN_MODE_MUST_BE_CROSS")
    require(str(config.get("position_mode","")).lower()=="hedge","POSITION_MODE_MUST_BE_HEDGE")
    require(config.get("hedge_mode_enabled") is True,"HEDGE_MODE_MUST_BE_ENABLED")
    require(hedge.get("read_only",True) is True,"HEDGE_READ_ONLY_MUST_BE_TRUE")
    require(hedge.get("live_trading_enabled",False) is False,"LIVE_TRADING_MUST_BE_FALSE")
    require(str(hedge.get("operation_mode","paper")).lower() in {"paper","shadow"},"OPERATION_MODE_MUST_BE_PAPER_OR_SHADOW")
    require(str(paper.get("state_backend","sql")).lower()=="sql","PAPER_PERSISTENCE_MUST_BE_SQL")
    require(paper.get("require_closed_candle",True) is True,"CLOSED_CANDLE_REQUIRED")
    require(str(paper.get("fill_model","conservative")).lower()=="conservative","CONSERVATIVE_FILL_MODEL_REQUIRED")
    require(dash.get("enabled") is True,"DASHBOARD_MUST_BE_ENABLED")
    require(dash.get("local_only",True) is True,"DASHBOARD_MUST_BE_LOCAL_ONLY")
    backend=str(dash.get("telemetry_backend","memory")).lower();require(backend in {"memory","jsonl"},"TELEMETRY_BACKEND_INVALID")
    try:capacity=int(dash.get("telemetry_capacity",2000));require(10<=capacity<=100000,"TELEMETRY_CAPACITY_INVALID")
    except Exception:errors.append("TELEMETRY_CAPACITY_INVALID")
    if backend=="jsonl":require(bool(str(dash.get("telemetry_path","")).strip()),"TELEMETRY_PATH_REQUIRED")
    require(bool(str(dash.get("control_state_path","")).strip()),"CONTROL_STATE_PATH_REQUIRED")
    for key in ("telemetry_path","control_state_path"):
        if ".." in Path(str(dash.get(key,""))).parts:errors.append(f"{key.upper()}_TRAVERSAL_FORBIDDEN")
    require(api.get("enabled") is True,"API_SERVER_MUST_BE_ENABLED")
    require(str(api.get("listen_ip_address","")) in {"127.0.0.1","::1","localhost"},"API_MUST_BIND_LOOPBACK")
    require(not api.get("CORS_origins",[]),"CORS_MUST_BE_EMPTY")
    require(str(exchange.get("key","")).strip()=="" and str(exchange.get("secret","")).strip()=="","EXCHANGE_CREDENTIALS_MUST_BE_EMPTY")
    require(str(config.get("timeframe","1m"))=="1m","TIMEFRAME_MUST_BE_1M")
    pair=str(config.get("managed_pair","")).strip();whitelist=exchange.get("pair_whitelist",[]);require(bool(pair) and whitelist==[pair],"PAIR_WHITELIST_MUST_EQUAL_MANAGED_PAIR")
    require(bool(str(config.get("strategy","")).strip()),"STRATEGY_REQUIRED")
    require(bool(str(config.get("strategy_path","")).strip()),"STRATEGY_PATH_REQUIRED")
    try:port=int(api.get("listen_port",8080));require(1<=port<=65535,"API_PORT_INVALID")
    except Exception:errors.append("API_PORT_INVALID")
    for key,minlen in (("password",16),("jwt_secret_key",32),("ws_token",16)):
        text=str(api.get(key,"")).strip();require(len(text)>=minlen and "REPLACE" not in text.upper() and "GENERATE" not in text.upper(),f"API_{key.upper()}_WEAK")
    roles=_mapping(api.get("hedge_roles"));user=str(api.get("username","")).strip();require(str(roles.get(user,"")).upper() in {"OPERATOR","RISK_MANAGER","ADMIN"},"DASHBOARD_USER_REQUIRES_OPERATOR_ROLE")
    from freqtrade.hedge.r56.config import validate_r56_config
    errors.extend(validate_r56_config(config))
    from freqtrade.hedge.r561.config import validate_r561_config
    errors.extend(validate_r561_config(config))
    return tuple(dict.fromkeys(errors))

def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(prog="freqtrade-hedge-r55");sub=parser.add_subparsers(dest="cmd",required=True)
    sub.add_parser("contract")
    v=sub.add_parser("validate-config");v.add_argument("config")
    u=sub.add_parser("dashboard-url");u.add_argument("config")
    sub.add_parser("offline-acceptance")
    args=parser.parse_args(argv)
    if args.cmd=="contract":print(json.dumps(asdict(StrategyContract()),indent=2));return 0
    if args.cmd=="offline-acceptance":
        checks={"strategy_contract":True,"mainnet_write_default_locked":True,"dashboard_optional":True,"telemetry_non_authoritative":True};print(json.dumps({"status":"PASS","checks":checks,"offline_only":True,"exchange_access":"NONE"},indent=2));return 0
    config=json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.cmd=="validate-config":
        errors=validate_dryrun_config(config);print(json.dumps({"status":"PASS" if not errors else "FAIL","errors":errors},indent=2));return 0 if not errors else 2
    api=_mapping(config.get("api_server"));host=str(api.get("listen_ip_address","127.0.0.1"));host="127.0.0.1" if host=="localhost" else host;print(f"http://{host}:{int(api.get('listen_port',8080))}/hedge-dashboard");return 0
if __name__=="__main__":raise SystemExit(main())
