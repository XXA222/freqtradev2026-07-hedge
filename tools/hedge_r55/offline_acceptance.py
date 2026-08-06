from __future__ import annotations
import json, secrets, sys, tempfile
from pathlib import Path

def project_root()->Path:
    here=Path(__file__).resolve()
    for p in (here,*here.parents):
        if (p/"pyproject.toml").exists() and (p/"freqtrade").is_dir():return p
    raise RuntimeError("project root not found")
ROOT=project_root();sys.path.insert(0,str(ROOT))
from freqtrade.hedge.r55.cli import validate_dryrun_config
from freqtrade.hedge.r55.strategy import StrategyContract

def main()->int:
    checks={}
    checks["strategy_contract"]=StrategyContract().schema_version==1
    config=json.loads((ROOT/"config_examples/config_hedge_r55_dryrun_ui.template.json").read_text(encoding="utf-8"))
    config["api_server"]["password"]=secrets.token_urlsafe(24);config["api_server"]["jwt_secret_key"]=secrets.token_urlsafe(48);config["api_server"]["ws_token"]=secrets.token_urlsafe(32)
    checks["safe_config_template"]=not validate_dryrun_config(config)
    checks["ui_assets"]=all((ROOT/"freqtrade/rpc/api_server/hedge_ui"/n).is_file() for n in ("index.html","app.js","styles.css"))
    checks["mainnet_writes_locked"]=config["hedge"]["read_only"] is True and config["hedge"]["live_trading_enabled"] is False and config["dry_run"] is True
    failed=[k for k,v in checks.items() if not v]
    print(json.dumps({"release":"R5.5.2","status":"PASS" if not failed else "FAIL","checks":checks,"failed":failed,"offline_only":True,"exchange_access":"NONE","real_order_requests":0},indent=2))
    return 0 if not failed else 2
if __name__=="__main__":raise SystemExit(main())
