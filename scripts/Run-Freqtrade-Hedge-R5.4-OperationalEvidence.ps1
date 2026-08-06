param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$AccountId,

    [ValidateSet("init-store", "evaluate-all", "evaluate-stage", "verify-bundle")]
    [string]$Command = "evaluate-all",

    [string]$Stage = "MAINNET_READONLY",
    [string]$BundlePath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python not found: $Python"
}

$ArtifactRoot = Join-Path $ProjectRoot "artifacts\hedge_r54"
$Database = Join-Path $ArtifactRoot "r54-authority.sqlite"
New-Item -ItemType Directory -Path $ArtifactRoot -Force | Out-Null

switch ($Command) {
    "init-store" {
        & $Python -m freqtrade.hedge.r54 init-store --database $Database
    }
    "evaluate-all" {
        $Output = Join-Path $ArtifactRoot "R5.4-READINESS.json"
        & $Python -m freqtrade.hedge.r54 evaluate-all `
            --database $Database `
            --account-id $AccountId `
            --output $Output
    }
    "evaluate-stage" {
        $Output = Join-Path $ArtifactRoot ("R5.4-{0}-DECISION.json" -f $Stage)
        & $Python -m freqtrade.hedge.r54 evaluate-stage `
            --database $Database `
            --account-id $AccountId `
            --stage $Stage `
            --output $Output
    }
    "verify-bundle" {
        if (-not $BundlePath) {
            throw "BundlePath is required for verify-bundle."
        }
        & $Python -m freqtrade.hedge.r54 verify-bundle --bundle $BundlePath
    }
}

exit $LASTEXITCODE
