param(
    [Parameter(Mandatory = $false)]
    [string]$ProjectRoot = "D:\Program Files\freqtradev2026.07-hedge",

    [Parameter(Mandatory = $false)]
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Project root not found: $ProjectRoot"
}

if (-not $Python) {
    $Candidates = @(
        (Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"),
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe")
    )
    $Python = $Candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python was not found."
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Output = Join-Path $ProjectRoot "artifacts\hedge_r531_diagnostics\$Timestamp"
New-Item -ItemType Directory -Path $Output -Force | Out-Null

$OldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = @(
    $ProjectRoot,
    (Join-Path $ProjectRoot "tools\hedge_r53"),
    (Join-Path $ProjectRoot "tools\hedge_r531"),
    $OldPythonPath
) -join [IO.Path]::PathSeparator
$env:PYTHONUTF8 = "1"

try {
    Push-Location $ProjectRoot
    & $Python -m pytest -q tools/hedge_r531/tests
    if ($LASTEXITCODE -ne 0) { throw "R5.3.1 tests failed." }

    & $Python -m tools.hedge_r531.r531_toolkit.offline_acceptance `
        --project-root $ProjectRoot `
        --output (Join-Path $Output "R5.3.1-SAFETY-CLOSURE-READINESS.json")
    if ($LASTEXITCODE -ne 0) { throw "R5.3.1 offline acceptance failed." }

    & $Python -m tools.hedge_r53.r53_toolkit.diagnostics `
        --project-root $ProjectRoot `
        --python $Python `
        --output-dir $Output
    if ($LASTEXITCODE -ne 0) { throw "Full R5.3 diagnostics failed." }

    Write-Host "R5.3.1 diagnostics PASS: $Output" -ForegroundColor Green
}
finally {
    Pop-Location
    $env:PYTHONPATH = $OldPythonPath
}
