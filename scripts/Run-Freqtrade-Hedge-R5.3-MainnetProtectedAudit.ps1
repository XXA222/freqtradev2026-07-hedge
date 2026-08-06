[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$LegacyCredentialFile,

    [string]$ProxyUrl = ""
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
$Code = "R530P"
$OutputRoot = Join-Path $ProjectRoot "artifacts\hedge_r530_mainnet_protected\$Stamp-$Code"
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$Report = Join-Path $OutputRoot "R5.3-MAINNET-PROTECTED-AUDIT.json"
$Stdout = Join-Path $OutputRoot "mainnet-protected.stdout.txt"
$Stderr = Join-Path $OutputRoot "mainnet-protected.stderr.txt"

$Arguments = @(
    "-m", "tools.hedge_r53.r53_toolkit",
    "mainnet-protected-audit",
    "--project-root", $ProjectRoot,
    "--credentials", $LegacyCredentialFile,
    "--output", $Report
)
if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
    $Arguments += @("--proxy-url", $ProxyUrl)
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$ProjectRoot;$($ProjectRoot)\tools;$($ProjectRoot)\tools\hedge_r53"
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$global:LASTEXITCODE = 0
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
    if ($Payload.status -ne "R5_3_MAINNET_PROTECTED_AUDIT_PASS_WRITES_LOCKED") {
        $FinalCode = 2
    }
    if ([int64]$Payload.real_mainnet_order_requests_sent -ne 0) {
        $FinalCode = 2
    }
    if ($Payload.exchange_write -ne "LOCKED") {
        $FinalCode = 2
    }
    Write-Host "R5.3.0 mainnet protected audit status: $($Payload.status)"
    Write-Host "R5.3.0 ready for exchange write: $($Payload.ready_for_exchange_write)"
}
else {
    $FinalCode = 2
}

$Downloads = Join-Path $env:USERPROFILE "Downloads"
New-Item -ItemType Directory -Path $Downloads -Force | Out-Null
$Upload = Join-Path $Downloads "Freqtrade_Hedge_R5.3_MainnetProtectedUpload_$Stamp-$Code.zip"
if (Test-Path -LiteralPath $Upload) {
    Remove-Item -LiteralPath $Upload -Force
}
Compress-Archive -Path (Join-Path $OutputRoot "*") -DestinationPath $Upload -Force

Write-Host "R5.3.0 protected audit output directory: $OutputRoot"
Write-Host "R5.3.0 protected audit upload ZIP: $Upload"
Write-Host "Mainnet order requests sent by protected audit: 0"
Write-Host "Exchange order write: LOCKED"

Set-Variable -Name LASTEXITCODE -Scope Global -Value $FinalCode
