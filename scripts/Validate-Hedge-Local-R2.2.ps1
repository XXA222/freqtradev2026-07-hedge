param([string]$ProjectRoot = "D:\Program Files\freqtrade-hedge")
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BacktestConfig = Join-Path $ProjectRoot "config_examples\config_hedge_backtest.example.json"
$PaperConfig = Join-Path $ProjectRoot "config_examples\config_hedge_paper.example.json"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
foreach ($Path in @($BacktestConfig, $PaperConfig)) { if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Config not found: $Path" } }
$env:HEDGE_R22_BACKTEST_CONFIG = $BacktestConfig
$env:HEDGE_R22_PAPER_CONFIG = $PaperConfig
$Code = @'
import json
import os
from pathlib import Path
from freqtrade.commands.hedge_runtime_commands import _validate_paper_runtime_config
from freqtrade.hedge.config import validate_hedge_config

backtest = json.loads(Path(os.environ["HEDGE_R22_BACKTEST_CONFIG"]).read_text(encoding="utf-8"))
paper = json.loads(Path(os.environ["HEDGE_R22_PAPER_CONFIG"]).read_text(encoding="utf-8"))
validated_backtest = validate_hedge_config(backtest)
validated_paper = validate_hedge_config(paper)
assert validated_backtest.paper is not None
assert validated_paper.paper is not None
assert validated_paper.paper.max_catchup_candles > 0
_validate_paper_runtime_config(paper)
print("R2.2 typed Hedge configs: PASS")
'@
Push-Location -LiteralPath $ProjectRoot
try {
    & $Python -c $Code
    if ($LASTEXITCODE -ne 0) { throw "R2.2 configuration validation failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
    Remove-Item Env:HEDGE_R22_BACKTEST_CONFIG -ErrorAction SilentlyContinue
    Remove-Item Env:HEDGE_R22_PAPER_CONFIG -ErrorAction SilentlyContinue
}
& (Join-Path $ProjectRoot "scripts\Debug-Hedge-Local-R2.2.ps1") -ProjectRoot $ProjectRoot
