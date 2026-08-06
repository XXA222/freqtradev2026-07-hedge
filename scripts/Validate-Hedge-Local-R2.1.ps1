param([string]$ProjectRoot = "D:\Program Files\freqtrade-hedge")
$ErrorActionPreference = "Stop"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BacktestConfig = Join-Path $ProjectRoot "config_examples\config_hedge_backtest.example.json"
$PaperConfig = Join-Path $ProjectRoot "config_examples\config_hedge_paper.example.json"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
foreach ($Path in @($BacktestConfig, $PaperConfig)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Config not found: $Path" }
}
& $Python -c "from pathlib import Path; import json; from freqtrade.hedge.config import validate_hedge_config; from freqtrade.commands.hedge_runtime_commands import _validate_paper_runtime_config; b=json.loads(Path(r'$($BacktestConfig.Replace("'", "''"))').read_text(encoding='utf-8')); p=json.loads(Path(r'$($PaperConfig.Replace("'", "''"))').read_text(encoding='utf-8')); assert validate_hedge_config(b).paper is not None; _validate_paper_runtime_config(p); print('R2.1 local Hedge configs: PASS')"
if ($LASTEXITCODE -ne 0) { throw "Hedge local configuration validation failed with exit code $LASTEXITCODE" }
& $Python -m pytest -q --tb=short --timeout=120 -o addopts= tests\hedge\integration\test_local_backtest_paper_r21.py tests\hedge\backtesting\test_parity_reports.py tests\hedge\simulation\test_wallet_matcher.py
if ($LASTEXITCODE -ne 0) { throw "R2.1 local Hedge validation tests failed with exit code $LASTEXITCODE" }
