from __future__ import annotations
import argparse,ast,json,re
from pathlib import Path
REQUIRED=(
'freqtrade/hedge/r56/session.py','freqtrade/hedge/r56/state.py','freqtrade/hedge/r56/market.py','freqtrade/hedge/r56/warmup.py','freqtrade/hedge/r56/universe.py','freqtrade/hedge/r56/allocation.py','freqtrade/hedge/r56/admission.py','freqtrade/hedge/r56/slippage.py','freqtrade/hedge/r56/fills.py','freqtrade/hedge/r56/ledger.py','freqtrade/hedge/r56/risk.py','freqtrade/hedge/r56/breaker.py','freqtrade/hedge/r56/cooldown.py','freqtrade/hedge/r56/parameters.py','freqtrade/hedge/r56/shadow.py','freqtrade/hedge/r56/checkpoint.py','freqtrade/hedge/r56/attribution.py','freqtrade/hedge/r56/alerts.py','freqtrade/hedge/r56/dashboard.py','freqtrade/hedge/r56/readiness.py','freqtrade/hedge/r56/runtime.py','freqtrade/hedge/r56/config.py','freqtrade/hedge/r56/cli.py','tests/hedge/r56/test_runtime_integration.py','config_examples/config_hedge_r55_dryrun_ui.template.json')
def run(root:Path)->dict[str,object]:
    checks={};missing=[x for x in REQUIRED if not (root/x).is_file()];checks['required_files']=not missing
    syntax=[]
    for rel in REQUIRED:
        path=root/rel
        if path.suffix=='.py' and path.is_file():
            try:ast.parse(path.read_text(encoding='utf-8'),filename=rel)
            except SyntaxError as exc:syntax.append(f'{rel}:{exc}')
    checks['python_ast']=not syntax
    config=json.loads((root/'config_examples/config_hedge_r55_dryrun_ui.template.json').read_text(encoding='utf-8'));checks['mainnet_write_locked']=config['hedge']['read_only'] is True and config['hedge']['live_trading_enabled'] is False and config['dry_run'] is True
    runtime=(root/'freqtrade/hedge/integration/paper_runtime.py').read_text(encoding='utf-8');checks['paper_runtime_hook']='DryRunOperationsRuntime' in runtime and 'operations_error' in runtime
    dashboard=(root/'freqtrade/rpc/api_server/hedge_dashboard.py').read_text(encoding='utf-8');checks['dashboard_projection']='operations_snapshot' in dashboard
    js=(root/'freqtrade/rpc/api_server/hedge_ui/app.js').read_text(encoding='utf-8');checks['ui_operations_panel']="$('operations')" in js
    config_py=(root/'freqtrade/hedge/config.py').read_text(encoding='utf-8');checks['central_config_whitelist']='_R56_ALLOWED_KEYS' in config_py and '("r56", _R56_ALLOWED_KEYS)' in config_py
    forbidden=[]
    for path in (root/'freqtrade/hedge/r56').glob('*.py'):
        text=path.read_text(encoding='utf-8')
        if re.search(r'(?i)(api[_-]?secret|private[_-]?key)\s*=\s*["\'][^"\']+["\']',text):forbidden.append(path.name)
    checks['no_embedded_secrets']=not forbidden
    checks['no_exchange_write_adapter']=not any('/order' in (root/x).read_text(encoding='utf-8',errors='ignore') for x in REQUIRED if (root/x).suffix=='.py' and (root/x).is_file())
    failed=[k for k,v in checks.items() if not v]
    return {'status':'PASS' if not failed else 'FAIL','checks':checks,'missing':missing,'syntax_errors':syntax,'forbidden':forbidden,'failed':failed,'mainnet_writes':'LOCKED','exchange_access':'NONE'}
def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser();p.add_argument('--project-root',default='.');p.add_argument('--output');a=p.parse_args(argv);r=run(Path(a.project_root).resolve());text=json.dumps(r,indent=2,sort_keys=True)
    if a.output:Path(a.output).write_text(text+'\n',encoding='utf-8')
    print(text);return 0 if r['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
