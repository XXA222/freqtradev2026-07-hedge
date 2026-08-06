param([string]$ProjectRoot = "D:\Program Files\freqtrade-hedge")
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $ProjectRoot "config_examples\config_hedge_paper.example.json"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw "Config not found: $Config" }
Push-Location -LiteralPath $ProjectRoot
try {
    & $Python -m freqtrade hedge-paper --config $Config --strategy HedgeDualEmaExample --strategy-path (Join-Path $ProjectRoot "config_examples\strategies")
    if ($LASTEXITCODE -ne 0) { throw "Hedge Paper failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
