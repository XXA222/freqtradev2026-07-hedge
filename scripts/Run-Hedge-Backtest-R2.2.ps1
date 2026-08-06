param(
    [string]$ProjectRoot = "D:\Program Files\freqtrade-hedge",
    [string]$Timerange = "20260101-20260801"
)
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $ProjectRoot "config_examples\config_hedge_backtest.example.json"
$Output = Join-Path $ProjectRoot "user_data\backtest_results\hedge-r2.2.json"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw "Config not found: $Config" }
New-Item -ItemType Directory -Path (Split-Path -Parent $Output) -Force | Out-Null
Push-Location -LiteralPath $ProjectRoot
try {
    & $Python -m freqtrade hedge-backtesting --config $Config --timerange $Timerange --strategy HedgeDualEmaExample --strategy-path (Join-Path $ProjectRoot "config_examples\strategies") --hedge-export-filename $Output
    if ($LASTEXITCODE -ne 0) { throw "Hedge backtest failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
if (-not (Test-Path -LiteralPath $Output -PathType Leaf)) { throw "Hedge backtest result was not created: $Output" }
if (-not (Test-Path -LiteralPath "$Output.sha256" -PathType Leaf)) { throw "Hedge backtest SHA256 sidecar was not created: $Output.sha256" }
Write-Host "Hedge backtest result: $Output" -ForegroundColor Green
Write-Host "Hedge backtest checksum: $Output.sha256" -ForegroundColor Green
