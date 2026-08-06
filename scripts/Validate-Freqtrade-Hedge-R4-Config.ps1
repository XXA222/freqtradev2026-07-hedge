[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$ConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python was not found: $Python"
}
$Previous = $env:PYTHONPATH
$env:PYTHONPATH = $ProjectRoot
try {
    & $Python -m freqtrade.hedge.r4 validate-config --config $ConfigPath --project-root $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "R4 configuration validation failed."
    }
}
finally {
    if ($null -eq $Previous) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $Previous
    }
}
