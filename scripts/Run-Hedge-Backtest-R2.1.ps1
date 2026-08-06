param(
    [string]$ProjectRoot = "D:\Program Files\freqtrade-hedge",
    [string]$Timerange = "20260101-20260801"
)
$ErrorActionPreference = "Stop"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $ProjectRoot "config_examples\config_hedge_backtest.example.json"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw "Config not found: $Config" }
& $Python -m freqtrade hedge-backtesting --config $Config --timerange $Timerange --strategy HedgeDualEmaExample --strategy-path (Join-Path $ProjectRoot "config_examples\strategies") --hedge-export-filename (Join-Path $ProjectRoot "user_data\backtest_results\hedge-r2.1.json")
if ($LASTEXITCODE -ne 0) { throw "Hedge backtest failed with exit code $LASTEXITCODE" }
