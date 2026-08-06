[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [switch]$IncludeFullHedgeRegression
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$PythonCandidates = @(
    (Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"),
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot "venv\Scripts\python.exe")
)
$Python = $PythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if ($null -eq $Python) {
    throw "Project Python was not found."
}
$ReportRoot = Join-Path $ProjectRoot "artifacts\hedge_r381_production_runtime"
$Reports = @(Get-ChildItem -LiteralPath $ReportRoot -Recurse -File -Filter "R3.8.1-PRODUCTION-RUNTIME-READINESS.json" -ErrorAction SilentlyContinue)
$R381Report = $Reports | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $R381Report) {
    throw "R3.8.1 readiness report was not found."
}
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Unique = [guid]::NewGuid().ToString("N").Substring(0, 8)
$Output = Join-Path $ProjectRoot ("artifacts\hedge_r401_diagnostics\" + $Stamp + "-707E-" + $Unique)
New-Item -ItemType Directory -Path $Output -Force | Out-Null
$Previous = $env:PYTHONPATH
$env:PYTHONPATH = $ProjectRoot
$ExitCode = 98
try {
    $Arguments = @(
        "-m", "tools.hedge_r40.r40_toolkit.full_diagnostics",
        "--project-root", $ProjectRoot,
        "--package-root", $ProjectRoot,
        "--output-dir", $Output,
        "--r381-report", $R381Report.FullName
    )
    if ($IncludeFullHedgeRegression) {
        $Arguments += "--include-full-hedge"
    }
    try {
        & $Python @Arguments
        $ExitCode = $LASTEXITCODE
    }
    catch {
        Set-Content -LiteralPath (Join-Path $Output "DIAGNOSTIC-LAUNCH-ERROR.txt") -Value $_.Exception.Message -Encoding Ascii
        $ExitCode = 97
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
$Downloads = Join-Path $env:USERPROFILE "Downloads"
New-Item -ItemType Directory -Path $Downloads -Force | Out-Null
$Upload = Join-Path $Downloads ("Freqtrade_Hedge_R4.0.1_DiagnosticUpload_" + $Stamp + "-707E.zip")
Compress-Archive -LiteralPath (Join-Path $Output "*") -DestinationPath $Upload -CompressionLevel Optimal -Force
$UploadHash = (Get-FileHash -LiteralPath $Upload -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($Upload + ".sha256.txt") -Value $UploadHash -Encoding Ascii
Write-Host "R4.0.1 diagnostics: $Output"
Write-Host "Upload ZIP: $Upload"
Write-Host "Upload ZIP SHA256: $UploadHash"
if ($ExitCode -ne 0) {
    throw "R4.0.1 aggregate diagnostics found failures. Upload the single ZIP from Downloads."
}
