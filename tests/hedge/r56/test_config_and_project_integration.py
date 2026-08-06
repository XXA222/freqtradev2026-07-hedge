from __future__ import annotations
import ast,json
from pathlib import Path
from freqtrade.hedge.r56.config import validate_r56_config

ROOT=Path(__file__).resolve().parents[3]

def test_r56_template_is_safe_and_valid():
    config=json.loads((ROOT/'config_examples/config_hedge_r55_dryrun_ui.template.json').read_text(encoding='utf-8'))
    assert validate_r56_config(config)==()
    assert config['hedge']['read_only'] is True
    assert config['hedge']['live_trading_enabled'] is False

def test_project_integration_surfaces_are_present_and_parseable():
    paths=(ROOT/'freqtrade/hedge/integration/paper_runtime.py',ROOT/'freqtrade/hedge/config.py',ROOT/'freqtrade/rpc/api_server/hedge_dashboard.py',ROOT/'freqtrade/rpc/api_server/hedge_dashboard_schemas.py')
    for path in paths:ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
    paper=paths[0].read_text(encoding='utf-8');assert 'DryRunOperationsRuntime' in paper and 'operations_error' in paper and 'self.operations.latest.new_risk_enabled' in paper
    cfg=paths[1].read_text(encoding='utf-8');assert '"r56"' in cfg and '_R56_ALLOWED_KEYS' in cfg
    dashboard=paths[2].read_text(encoding='utf-8');assert 'operations_snapshot' in dashboard
    js=(ROOT/'freqtrade/rpc/api_server/hedge_ui/app.js').read_text(encoding='utf-8');assert "$('operations')" in js and 'R5.6' not in js or 'operations' in js

def test_invalid_r56_config_fails_closed():
    bad={'dry_run':False,'hedge':{'read_only':False,'live_trading_enabled':True,'r56':{'enabled':True,'state_path':'../x','drawdown_warning':'0.2','drawdown_pause':'0.1'}}}
    errors=set(validate_r56_config(bad));assert {'R56_REQUIRES_DRY_RUN','R56_REQUIRES_READ_ONLY','R56_FORBIDS_LIVE_TRADING','R56_STATE_PATH_TRAVERSAL','R56_DRAWDOWN_THRESHOLDS_INVALID'}.issubset(errors)
