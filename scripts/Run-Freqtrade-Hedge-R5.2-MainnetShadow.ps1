[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$LegacyCredentialFile,

    [string]$ProxyUrl = "",

    [decimal]$LongSignal = 0,

    [decimal]$ShortSignal = 0,

    [decimal]$RealizedVolatility = 0,

    [decimal]$FundingRate = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python was not found: $Python"
}
if (-not (Test-Path -LiteralPath $LegacyCredentialFile -PathType Leaf)) {
    throw "Mainnet read-only credential file was not found."
}

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Code = "R523S"
$OutputRoot = Join-Path $ProjectRoot "artifacts\hedge_r523_mainnet_shadow\$Stamp-$Code"
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$Report = Join-Path $OutputRoot "R5.2-MAINNET-SHADOW-REPORT.json"
$Stdout = Join-Path $OutputRoot "mainnet-shadow.stdout.txt"
$Stderr = Join-Path $OutputRoot "mainnet-shadow.stderr.txt"

$Arguments = @(
    "-m", "tools.hedge_r52.r52_toolkit",
    "mainnet-shadow",
    "--project-root", $ProjectRoot,
    "--credentials", $LegacyCredentialFile,
    "--output", $Report,
    "--long-signal", ([string]$LongSignal),
    "--short-signal", ([string]$ShortSignal),
    "--realized-volatility", ([string]$RealizedVolatility),
    "--funding-rate", ([string]$FundingRate)
)
if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
    $Arguments += @("--proxy-url", $ProxyUrl)
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$ProjectRoot;$($ProjectRoot)\tools"
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python @Arguments 1> $Stdout 2> $Stderr
$CodeValue = [int]$LASTEXITCODE
$ErrorActionPreference = $OldPreference
if ($null -eq $PreviousPythonPath) {
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
}
else {
    $env:PYTHONPATH = $PreviousPythonPath
}

if (Test-Path -LiteralPath $Stdout) {
    Get-Content -LiteralPath $Stdout | Write-Host
}
if (Test-Path -LiteralPath $Stderr) {
    $ErrorText = Get-Content -LiteralPath $Stderr -Raw
    if (-not [string]::IsNullOrWhiteSpace($ErrorText)) {
        Write-Warning $ErrorText
    }
}

$FinalCode = $CodeValue
if ($CodeValue -eq 0 -and (Test-Path -LiteralPath $Report -PathType Leaf)) {
    $Payload = Get-Content -LiteralPath $Report -Raw | ConvertFrom-Json
    if ($Payload.status -ne "R5_2_PRODUCTION_STRATEGY_CORE_SHADOW_PASS") {
        $FinalCode = 2
    }
    if ([int64]$Payload.real_mainnet_order_requests_sent -ne 0) {
        $FinalCode = 2
    }
    Write-Host "R5.2.3 mainnet shadow status: $($Payload.status)"
}
else {
    $FinalCode = 2
}

$Downloads = Join-Path $env:USERPROFILE "Downloads"
New-Item -ItemType Directory -Path $Downloads -Force | Out-Null
$Upload = Join-Path $Downloads "Freqtrade_Hedge_R5.2.3_MainnetShadowUpload_$Stamp-$Code.zip"
if (Test-Path -LiteralPath $Upload) {
    Remove-Item -LiteralPath $Upload -Force
}
Compress-Archive -Path (Join-Path $OutputRoot "*") -DestinationPath $Upload -Force

Write-Host "R5.2.3 shadow output directory: $OutputRoot"
Write-Host "R5.2.3 shadow upload ZIP: $Upload"
Write-Host "Mainnet order requests sent by shadow runner: 0"
Write-Host "Exchange order write: LOCKED"

Set-Variable -Name LASTEXITCODE -Scope Global -Value $FinalCode
