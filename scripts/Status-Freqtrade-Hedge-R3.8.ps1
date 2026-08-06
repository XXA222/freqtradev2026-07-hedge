[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$DeploymentConfig
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$DeploymentConfig = [System.IO.Path]::GetFullPath($DeploymentConfig)
$PythonCandidates = @(
    (Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"),
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot "venv\Scripts\python.exe")
)
$Python = $PythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if ($null -eq $Python) {
    throw "Project Python virtual environment was not found."
}
$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $ProjectRoot
try {
    & $Python -m freqtrade.hedge.deployment --deployment-config $DeploymentConfig status
    exit $LASTEXITCODE
}
finally {
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
}
