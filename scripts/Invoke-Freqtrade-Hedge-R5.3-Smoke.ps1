[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python was not found: $Python"
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$ProjectRoot;$($ProjectRoot)\tools;$($ProjectRoot)\tools\hedge_r53"
try {
    & $Python -m compileall -q freqtrade/hedge/r5 tools/hedge_r53
    if ($LASTEXITCODE -ne 0) { throw "R5.3 compile smoke failed." }
    & $Python -m pytest -q tools/hedge_r53/tests
    if ($LASTEXITCODE -ne 0) { throw "R5.3 test smoke failed." }
    Write-Host "R5.3 smoke: PASS" -ForegroundColor Green
}
finally {
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
}
