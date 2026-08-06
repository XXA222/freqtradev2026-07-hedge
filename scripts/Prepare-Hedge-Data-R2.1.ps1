param(
    [string]$ProjectRoot = "D:\Program Files\freqtrade-hedge",
    [string]$Timerange = "20260101-20260801",
    [string[]]$Timeframes = @("5m")
)
$ErrorActionPreference = "Stop"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $ProjectRoot "config_examples\config_hedge_backtest.example.json"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw "Config not found: $Config" }
$Args = @("-m", "freqtrade", "download-data", "--config", $Config, "--timerange", $Timerange, "--timeframes") + $Timeframes
& $Python @Args
if ($LASTEXITCODE -ne 0) { throw "Hedge data download failed with exit code $LASTEXITCODE" }
& $Python -m freqtrade list-data --config $Config --show-timerange
if ($LASTEXITCODE -ne 0) { throw "Downloaded-data verification failed with exit code $LASTEXITCODE" }
