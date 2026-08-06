param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$AccountId,

    [Parameter(Mandatory = $true)]
    [string]$ObservationsPath,

    [double]$ExpectedIntervalSeconds = 60.0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
$ArtifactRoot = Join-Path $ProjectRoot "artifacts\hedge_r54\evidence"
New-Item -ItemType Directory -Path $ArtifactRoot -Force | Out-Null

& $Python -m freqtrade.hedge.r54 evaluate-soak `
    --observations $ObservationsPath `
    --account-id $AccountId `
    --output-24h (Join-Path $ArtifactRoot "MAINNET_READONLY_24H.json") `
    --output-72h (Join-Path $ArtifactRoot "MAINNET_READONLY_72H.json") `
    --expected-interval-seconds $ExpectedIntervalSeconds

exit $LASTEXITCODE
