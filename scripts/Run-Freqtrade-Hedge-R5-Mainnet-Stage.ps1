param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [Parameter(Mandatory = $true)]
    [ValidateSet("MAINNET_READONLY", "ORDER_TEST", "SUBMIT_CANCEL", "SINGLE_LEG", "DUAL_LEG", "RESTRICTED_AUTO", "EXPANDED_LIMITS")]
    [string]$Stage,

    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [string]$Actor,

    [ValidateSet("BTCUSDT", "ETHUSDT")]
    [string]$Symbol = "ETHUSDT",

    [ValidateSet("LONG", "SHORT")]
    [string]$PositionSide = "LONG",

    [string]$TargetNotional = "10",
    [int]$MaxCycles = 10,
    [string]$OperatorTokenFile,
    [string]$RiskTokenFile,
    [string]$ConfirmationFile,
    [string]$PromotionTokenFile,
    [string]$PromotionManifest,
    [string]$AutoBridge,
    [string]$ProxyUrl,
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Python = Join-Path $Root "freqtrade-hedge\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project virtual-environment Python was not found: $Python"
}
if (-not $OutputPath) {
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path $Root "artifacts\hedge_r5_mainnet\$Stage-$Stamp-$RunId.json"
}
$OutputParent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Path $OutputParent -Force | Out-Null

$Arguments = @(
    "-m", "freqtrade.hedge.r5",
    "--project-root", $Root,
    "--config", $ConfigPath,
    "--stage", $Stage,
    "--run-id", $RunId,
    "--actor", $Actor,
    "--symbol", $Symbol,
    "--position-side", $PositionSide,
    "--target-notional", $TargetNotional,
    "--max-cycles", [string]$MaxCycles,
    "--output", $OutputPath
)
if ($OperatorTokenFile) { $Arguments += @("--operator-token-file", $OperatorTokenFile) }
if ($RiskTokenFile) { $Arguments += @("--risk-token-file", $RiskTokenFile) }
if ($ConfirmationFile) { $Arguments += @("--confirmation-file", $ConfirmationFile) }
if ($PromotionTokenFile) { $Arguments += @("--promotion-token-file", $PromotionTokenFile) }
if ($PromotionManifest) { $Arguments += @("--promotion-manifest", $PromotionManifest) }
if ($AutoBridge) { $Arguments += @("--auto-bridge", $AutoBridge) }
if ($ProxyUrl) { $Arguments += @("--proxy-url", $ProxyUrl) }

Push-Location $Root
try {
    & $Python @Arguments
    $Code = $LASTEXITCODE
} finally {
    Pop-Location
}
Write-Host "R5 stage report: $OutputPath"
exit $Code
