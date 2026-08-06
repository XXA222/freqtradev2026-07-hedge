from __future__ import annotations
import ast, hashlib, json, re, sys
from pathlib import Path

def root()->Path:
    h=Path(__file__).resolve()
    for p in (h,*h.parents):
        if (p/"pyproject.toml").exists() and (p/"freqtrade").is_dir():return p
    raise RuntimeError("project root not found")
R=root()
REQUIRED=("freqtrade/hedge/r55/strategy.py","freqtrade/hedge/r55/control.py","freqtrade/hedge/r55/telemetry.py","freqtrade/hedge/r55/cli.py","freqtrade/rpc/api_server/hedge_dashboard.py","freqtrade/rpc/api_server/hedge_dashboard_schemas.py","freqtrade/rpc/api_server/hedge_ui/index.html","freqtrade/rpc/api_server/hedge_ui/app.js","freqtrade/rpc/api_server/hedge_ui/styles.css","freqtrade/strategy/hedge.py","config_examples/config_hedge_r55_dryrun_ui.template.json","config_examples/strategies/HedgeAdaptiveRegimeStrategy.py","scripts/Run-Freqtrade-Hedge-R5.5-DryRun-UI.ps1","docs/hedge-r5.5-dryrun-ui-strategy-integration.md")
PYTHON=("freqtrade/hedge/r55/strategy.py","freqtrade/hedge/r55/control.py","freqtrade/hedge/r55/telemetry.py","freqtrade/hedge/r55/cli.py","freqtrade/rpc/api_server/hedge_dashboard.py","freqtrade/rpc/api_server/hedge_dashboard_schemas.py","freqtrade/strategy/hedge.py","config_examples/strategies/HedgeAdaptiveRegimeStrategy.py","freqtrade/hedge/integration/signal_provider.py","freqtrade/hedge/integration/paper_runtime.py","freqtrade/hedge/integration/production_context.py","freqtrade/hedge/integration/production_main_loop.py","freqtrade/hedge/integration/production_controller.py","freqtrade/hedge/simulation/exchange.py","freqtrade/hedge/simulation/replay.py","freqtrade/optimize/hedge_backtesting.py")
def main()->int:
    checks={}
    checks["required_files"]=all((R/x).is_file() for x in REQUIRED)
    try:
        for x in PYTHON:ast.parse((R/x).read_text(encoding="utf-8"),filename=x)
        checks["python_ast"]=True
    except Exception:checks["python_ast"]=False
    cfg=json.loads((R/"config_examples/config_hedge_r55_dryrun_ui.template.json").read_text())
    checks["safe_config"]=cfg.get("dry_run") is True and cfg["hedge"]["read_only"] is True and cfg["hedge"]["live_trading_enabled"] is False and cfg["api_server"]["listen_ip_address"]=="127.0.0.1" and not cfg["exchange"]["key"] and not cfg["exchange"]["secret"]
    html=(R/"freqtrade/rpc/api_server/hedge_ui/index.html").read_text();js=(R/"freqtrade/rpc/api_server/hedge_ui/app.js").read_text();plugin=(R/"freqtrade/rpc/api_server/hedge_plugin.py").read_text()
    checks["ui_security"]="localStorage" not in js and "local_only" in plugin and "/hedge-dashboard" in html+plugin
    strategy=(R/"freqtrade/hedge/r55/strategy.py").read_text();checks["strategy_safety"]="planner_config_for_directive" in strategy and "min(directive.confidence,directive.risk_scale)" in strategy.replace(" ","")
    parity="\n".join((R/x).read_text() for x in ("freqtrade/hedge/integration/signal_provider.py","freqtrade/hedge/integration/paper_runtime.py","freqtrade/hedge/integration/production_context.py","freqtrade/hedge/simulation/replay.py","freqtrade/optimize/hedge_backtesting.py"));checks["runtime_parity"]=all(k in parity for k in ("target_net_ratio","long_exposure_scale","allow_new_risk"))
    production_controller=(R/"freqtrade/hedge/integration/production_controller.py").read_text(encoding="utf-8")
    optional_signal_fields=("target_net_ratio","confidence","risk_scale","long_exposure_scale","short_exposure_scale","allow_new_risk","regime","strategy_reason")
    checks["legacy_signal_compatibility"]=all(f"signal.{field}" not in production_controller for field in optional_signal_fields) and all(f'getattr(signal, "{field}"' in production_controller for field in optional_signal_fields)
    telemetry=(R/"freqtrade/hedge/r55/telemetry.py").read_text();checks["telemetry_non_authoritative"]="except OSError" in telemetry and "self._record_error" in telemetry
    all_text="\n".join((R/x).read_text(errors="ignore") for x in REQUIRED if (R/x).suffix in {".py",".json",".md",".ps1",".js",".html"});checks["no_embedded_private_key"]="-----BEGIN PRIVATE KEY-----" not in all_text
    checks["packaging_assets"]="hedge_ui" in (R/"MANIFEST.in").read_text() and "freqtrade-hedge-r55" in (R/"pyproject.toml").read_text()
    failed=[k for k,v in checks.items() if not v];print(json.dumps({"release":"R5.5.2","status":"PASS" if not failed else "FAIL","checks":checks,"failed":failed},indent=2));return 0 if not failed else 2
if __name__=="__main__":raise SystemExit(main())
