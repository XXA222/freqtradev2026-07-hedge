param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$AccountId,

    [Parameter(Mandatory = $true)]
    [string]$ResultJson
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
$ArtifactRoot = Join-Path $ProjectRoot "artifacts\hedge_r54\evidence"
New-Item -ItemType Directory -Path $ArtifactRoot -Force | Out-Null

& $Python -m freqtrade.hedge.r54 build-postgres `
    --input $ResultJson `
    --account-id $AccountId `
    --output (Join-Path $ArtifactRoot "POSTGRES_MATRIX.json")

exit $LASTEXITCODE
