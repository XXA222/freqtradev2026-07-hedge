[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$SourceRoot = $PSScriptRoot,
    [switch]$SkipFullHedgeRegression
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-ProjectPython {
    param([string]$Root)
    $Candidates = @(
        (Join-Path $Root "freqtrade-hedge\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root "venv\Scripts\python.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    throw "Project Python executable was not found."
}

function Invoke-DiagnosticSection {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [string]$OutputDirectory
    )
    $Stdout = Join-Path $OutputDirectory "$Name.stdout.txt"
    $Stderr = Join-Path $OutputDirectory "$Name.stderr.txt"
    $Started = Get-Date
    & $script:Python @Arguments 1> $Stdout 2> $Stderr
    $Code = $LASTEXITCODE
    $Duration = ((Get-Date) - $Started).TotalSeconds
    return [pscustomobject]@{
        name = $Name
        exit_code = $Code
        passed = ($Code -eq 0)
        duration_seconds = [Math]::Round($Duration, 3)
        stdout = $Stdout
        stderr = $Stderr
        command = @($script:Python) + $Arguments
    }
}

$Root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Source = (Resolve-Path -LiteralPath $SourceRoot).Path
$Python = Resolve-ProjectPython -Root $Root
$script:Python = $Python
$ManifestPath = Join-Path $Source "SOURCE-MANIFEST.json"
$PayloadListPath = Join-Path $Source "R5.1-PAYLOAD-FILES.txt"
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "R5.1.3 source manifest is missing."
}
if (-not (Test-Path -LiteralPath $PayloadListPath -PathType Leaf)) {
    throw "R5.1.3 payload list is missing."
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($Entry in $Manifest.files) {
    $Path = Join-Path $Source ([string]$Entry.path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "R5.1 source file is missing: $($Entry.path)"
    }
    $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Hash -ne [string]$Entry.sha256) {
        throw "R5.1.3 source manifest mismatch: $($Entry.path)"
    }
}
Write-Host "R5.1.3 source manifest: PASS" -ForegroundColor Green

$Prerequisite = Get-ChildItem -LiteralPath (Join-Path $Root "artifacts\hedge_r501_combined") -Filter "R4-R5-DIAGNOSTIC-SUMMARY.json" -File -Recurse -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($null -eq $Prerequisite) {
    throw "A passing R4/R5 combined diagnostic report was not found."
}
$PrerequisitePayload = Get-Content -LiteralPath $Prerequisite.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$PrerequisitePayload.status -ne "R4_R5_FULL_DIAGNOSTICS_PASS") {
    throw "R4/R5 prerequisite is not passing: $($PrerequisitePayload.status)"
}
Write-Host "R4/R5 prerequisite: R4_R5_FULL_DIAGNOSTICS_PASS"

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Code = "6E4B"
$BackupRoot = Join-Path $Root "artifacts\hedge_r513_install_backups\r513-$Stamp-$Code"
$DiagnosticRoot = Join-Path $Root "artifacts\hedge_r513_install\$Stamp-$Code"
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
New-Item -ItemType Directory -Path $DiagnosticRoot -Force | Out-Null

$PayloadFiles = @(Get-Content -LiteralPath $PayloadListPath -Encoding ASCII | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$InstalledManifest = Join-Path $DiagnosticRoot "INSTALL-MANIFEST.files.jsonl"
foreach ($Relative in $PayloadFiles) {
    $SourceFile = Join-Path $Source $Relative
    $Destination = Join-Path $Root $Relative
    if (-not (Test-Path -LiteralPath $SourceFile -PathType Leaf)) {
        throw "R5.1 payload file is missing: $Relative"
    }
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $Backup = Join-Path $BackupRoot $Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $Backup) -Force | Out-Null
        Copy-Item -LiteralPath $Destination -Destination $Backup -Force
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $SourceFile -Destination $Destination -Force
    $Hash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    $Line = '{"path":"' + ($Relative.Replace('\', '\\')) + '","sha256":"' + $Hash + '"}'
    Add-Content -LiteralPath $InstalledManifest -Value $Line -Encoding ASCII
}
Write-Host "R5.1.3 cumulative source installation completed: $($PayloadFiles.Count) files"

$PreviousPythonPath = $env:PYTHONPATH
$PreviousWriteLock = $env:FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK
$env:FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK = "1"
$env:PYTHONPATH = @(
    $Root,
    (Join-Path $Root "tools\hedge_r24\guard"),
    (Join-Path $Root "tools\hedge_r24"),
    (Join-Path $Root "tools\hedge_r25"),
    (Join-Path $Root "tools\hedge_r30"),
    (Join-Path $Root "tools\hedge_r31"),
    (Join-Path $Root "tools\hedge_r32"),
    (Join-Path $Root "tools\hedge_r33"),
    (Join-Path $Root "tools\hedge_r34"),
    (Join-Path $Root "tools\hedge_r35"),
    (Join-Path $Root "tools\hedge_r36"),
    (Join-Path $Root "tools\hedge_r37"),
    (Join-Path $Root "tools\hedge_r38"),
    (Join-Path $Root "tools\hedge_r40"),
    (Join-Path $Root "tools\hedge_r50"),
    (Join-Path $Root "tools\hedge_r51")
) -join [IO.Path]::PathSeparator

$Sections = @()
try {
    Push-Location $Root
    try {
        $Sections += Invoke-DiagnosticSection -Name "01_pip_check" -Arguments @("-m", "pip", "check") -OutputDirectory $DiagnosticRoot
        $Sections += Invoke-DiagnosticSection -Name "02_compileall" -Arguments @("-m", "compileall", "-q", "freqtrade/hedge/r5", "tools/hedge_r50", "tools/hedge_r51") -OutputDirectory $DiagnosticRoot
        $Sections += Invoke-DiagnosticSection -Name "03_r51_tests" -Arguments @("-m", "pytest", "-q", "tools/hedge_r51/tests") -OutputDirectory $DiagnosticRoot
        $Sections += Invoke-DiagnosticSection -Name "04_r5_tests" -Arguments @("-m", "pytest", "-q", "tools/hedge_r50/tests") -OutputDirectory $DiagnosticRoot
        $Sections += Invoke-DiagnosticSection -Name "05_r4_tests" -Arguments @("-m", "pytest", "-q", "tools/hedge_r40/tests") -OutputDirectory $DiagnosticRoot
        $Sections += Invoke-DiagnosticSection -Name "06_execution_compatibility" -Arguments @(
            "-m", "pytest", "-q",
            "tools/hedge_r30/tests", "tools/hedge_r31/tests", "tools/hedge_r32/tests",
            "tools/hedge_r33/tests", "tools/hedge_r34/tests", "tools/hedge_r35/tests",
            "tools/hedge_r36/tests", "tools/hedge_r37/tests", "tools/hedge_r38/tests"
        ) -OutputDirectory $DiagnosticRoot
        if (-not $SkipFullHedgeRegression) {
            $Sections += Invoke-DiagnosticSection -Name "07_full_hedge_regression" -Arguments @("-m", "pytest", "-q", "tests/hedge") -OutputDirectory $DiagnosticRoot
        }
        $AcceptancePath = Join-Path $DiagnosticRoot "R5.1.3-OFFLINE-ACCEPTANCE.json"
        $Sections += Invoke-DiagnosticSection -Name "08_r51_offline_acceptance" -Arguments @(
            "-m", "tools.hedge_r51.r51_toolkit.offline_acceptance",
            "--project-root", $Root,
            "--output", $AcceptancePath
        ) -OutputDirectory $DiagnosticRoot
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
    $env:FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK = $PreviousWriteLock
}

$Failed = @($Sections | Where-Object { -not $_.passed })
$Summary = [ordered]@{
    schema_version = 1
    toolkit_version = "5.1.3"
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    status = $(if ($Failed.Count -eq 0) { "R5_1_INSTALL_DIAGNOSTICS_PASS" } else { "R5_1_INSTALL_DIAGNOSTICS_FAIL" })
    all_sections_attempted = $true
    failed_sections = @($Failed | ForEach-Object { $_.name })
    sections = $Sections
    prerequisite_report = $Prerequisite.FullName
    backup = $BackupRoot
    installer_exchange_access = "NONE"
    real_mainnet_order_requests_sent_by_installer = 0
}
$SummaryPath = Join-Path $DiagnosticRoot "R5.1.3-INSTALL-DIAGNOSTIC-SUMMARY.json"
$Summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryPath -Encoding ASCII
$TextPath = Join-Path $DiagnosticRoot "R5.1.3-INSTALL-DIAGNOSTIC-SUMMARY.txt"
@(
    "Status: $($Summary.status)",
    "All sections attempted: True",
    "Failed sections: $($Summary.failed_sections -join ',')",
    "Backup: $BackupRoot",
    "Installer exchange access: NONE",
    "Real mainnet order requests sent by installer: 0"
) | Set-Content -LiteralPath $TextPath -Encoding ASCII

$Downloads = Join-Path $env:USERPROFILE "Downloads"
$UploadZip = Join-Path $Downloads "Freqtrade_Hedge_R5.1.3_InstallDiagnosticUpload_$Stamp-$Code.zip"
if (Test-Path -LiteralPath $UploadZip) { Remove-Item -LiteralPath $UploadZip -Force }
Compress-Archive -Path (Join-Path $DiagnosticRoot "*") -DestinationPath $UploadZip -Force

Write-Host ""
Write-Host "R5.1.3 install diagnostics: $($Summary.status)"
Write-Host "R5.1.3 diagnostic directory: $DiagnosticRoot"
Write-Host "R5.1.3 diagnostic upload ZIP: $UploadZip"
Write-Host "Installation backup: $BackupRoot"
Write-Host "Installer exchange access: NONE"
Write-Host "Real mainnet order requests sent by installer: 0"
if ($Failed.Count -eq 0) {
    Write-Host "R5.1.3 MAINNET READONLY DEVELOPMENT: INSTALL_DIAGNOSTICS_PASS" -ForegroundColor Green
    exit 0
}
Write-Warning "R5.1.3 diagnostics found failures, but all sections were attempted."
exit 2
