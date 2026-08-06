[CmdletBinding()]
param(
    [string]$ProjectRoot = "D:\Program Files\freqtradev2026.07-hedge",
    [string]$ConfigPath = "",
    [switch]$ValidateOnly,
    [switch]$OpenDashboard
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
function New-RandomToken {
    param([int]$Bytes = 32)
    $Buffer = New-Object byte[] $Bytes
    $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Rng.GetBytes($Buffer) } finally { $Rng.Dispose() }
    return [Convert]::ToBase64String($Buffer).TrimEnd('=').Replace('+','-').Replace('/','_')
}
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { throw "Project root not found: $ProjectRoot" }
$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { $Python = "python" }
$Template = Join-Path $ProjectRoot "config_examples\config_hedge_r55_dryrun_ui.template.json"
if (-not (Test-Path -LiteralPath $Template -PathType Leaf)) { throw "R5.6 config template not found: $Template" }
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $ProjectRoot "user_data\config_hedge_r56_dryrun_operations.json" }
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $ConfigPath) -Force | Out-Null
    $Config = Get-Content -LiteralPath $Template -Raw -Encoding UTF8 | ConvertFrom-Json
    $Config.api_server.password = New-RandomToken -Bytes 24
    $Config.api_server.jwt_secret_key = New-RandomToken -Bytes 48
    $Config.api_server.ws_token = New-RandomToken -Bytes 32
    $Config | ConvertTo-Json -Depth 60 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
}
& $Python -m freqtrade.hedge.r55.cli validate-config $ConfigPath
if ($LASTEXITCODE -ne 0) { throw "R5.5/R5.6 combined config validation failed: $ConfigPath" }
& $Python -m freqtrade.hedge.r561 validate-config $ConfigPath --upstream-schema
if ($LASTEXITCODE -ne 0) { throw "R5.6.1 upstream/Hedge config validation failed: $ConfigPath" }
& $Python -m freqtrade.hedge.r56.cli validate-config $ConfigPath
if ($LASTEXITCODE -ne 0) { throw "R5.6 config validation failed: $ConfigPath" }
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Config.dry_run -ne $true -or $Config.hedge.read_only -ne $true -or $Config.hedge.live_trading_enabled -ne $false) { throw "R5.6 safety invariant failed: dry_run/read_only/live_trading_enabled" }
$Url = "http://$($Config.api_server.listen_ip_address):$($Config.api_server.listen_port)/hedge-dashboard"
Write-Host "";Write-Host "R5.6 DRY-RUN OPERATIONS READY" -ForegroundColor Green
Write-Host "Config:    $ConfigPath";Write-Host "Dashboard: $Url";Write-Host "User:      $($Config.api_server.username)";Write-Host "Password:  $($Config.api_server.password)";Write-Host "Mainnet writes remain LOCKED." -ForegroundColor Yellow
if ($ValidateOnly) { exit 0 }
if ($OpenDashboard) { Start-Process $Url }
Set-Location -LiteralPath $ProjectRoot
& $Python -m freqtrade trade --config $ConfigPath --strategy $Config.strategy --strategy-path $Config.strategy_path
exit $LASTEXITCODE
