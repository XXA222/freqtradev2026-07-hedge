param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
$OutputRoot = Join-Path $ProjectRoot "artifacts\hedge_r54\offline_acceptance"
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$Output = Join-Path $OutputRoot "R5.4-OFFLINE-ACCEPTANCE.json"

& $Python (Join-Path $ProjectRoot "tools\hedge_r54\offline_acceptance.py") $Output
exit $LASTEXITCODE
