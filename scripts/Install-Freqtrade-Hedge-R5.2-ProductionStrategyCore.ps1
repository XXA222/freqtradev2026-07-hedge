[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$SourceRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)

$RequiredProjectItems = @(
    (Join-Path $ProjectRoot "pyproject.toml"),
    (Join-Path $ProjectRoot "freqtrade"),
    (Join-Path $ProjectRoot "tests")
)
foreach ($Item in $RequiredProjectItems) {
    if (-not (Test-Path -LiteralPath $Item)) {
        throw "Invalid project root; missing: $Item"
    }
}

$Python = Join-Path $ProjectRoot "freqtrade-hedge\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python was not found: $Python"
}

$ManifestPath = Join-Path $SourceRoot "SOURCE-MANIFEST.json"
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "R5.2 source manifest was not found: $ManifestPath"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($Manifest.schema_version -ne 1 -or $Manifest.release -ne "R5.2.3") {
    throw "R5.2 source manifest identity is invalid."
}

$Entries = @($Manifest.files)
if ($Entries.Count -lt 1) {
    throw "R5.2 source manifest is empty."
}

foreach ($Entry in $Entries) {
    $Relative = [string]$Entry.path
    if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains("..")) {
        throw "Unsafe source manifest path: $Relative"
    }
    $SourcePath = Join-Path $SourceRoot ($Relative -replace '/', '\')
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "Manifest source file is missing: $Relative"
    }
    $Actual = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne ([string]$Entry.sha256).ToLowerInvariant()) {
        throw "Manifest SHA256 mismatch: $Relative"
    }
}
Write-Host "R5.2.3 source manifest: PASS ($($Entries.Count) files)" -ForegroundColor Green

$R516Root = Join-Path $ProjectRoot "artifacts\hedge_r516_mainnet_readonly"
if (-not (Test-Path -LiteralPath $R516Root -PathType Container)) {
    throw "R5.2 requires the R5.1.6 mainnet-readonly artifact directory: $R516Root"
}

# PowerShell 5.1 unwraps a one-item pipeline result into a scalar. Always force
# discovery results into an array before using Count or numeric indexing.
$R516Reports = @(
    Get-ChildItem `
        -LiteralPath $R516Root `
        -Filter "R5.1-MAINNET-READONLY-ACCEPTANCE.json" `
        -File `
        -Recurse `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
)
if (@($R516Reports).Count -eq 0) {
    throw "R5.2 requires a completed R5.1.6 mainnet-readonly acceptance report."
}

$R516 = $null
$R516ReportPath = $null
$RejectedR516Reports = New-Object System.Collections.Generic.List[string]
foreach ($CandidateReport in @($R516Reports)) {
    try {
        $Candidate = Get-Content -LiteralPath $CandidateReport.FullName -Raw | ConvertFrom-Json
        $RequiredFailures = @($Candidate.required_failures)
        $CandidatePass = (
            $Candidate.status -eq "R5_1_MAINNET_READONLY_COMPLETE_WRITES_LOCKED" -and
            [bool]$Candidate.mainnet_readonly_complete -and
            [int64]$Candidate.real_mainnet_order_requests_sent -eq 0 -and
            $Candidate.mainnet_live_exchange_write -eq "LOCKED" -and
            $RequiredFailures.Count -eq 0
        )
        if ($CandidatePass) {
            $R516 = $Candidate
            $R516ReportPath = $CandidateReport.FullName
            break
        }
        $RejectedR516Reports.Add("$($CandidateReport.FullName): acceptance contract did not pass")
    }
    catch {
        $RejectedR516Reports.Add("$($CandidateReport.FullName): $($_.Exception.Message)")
    }
}
if ($null -eq $R516) {
    $RejectedText = ($RejectedR516Reports -join " | ")
    throw "No valid completed R5.1.6 mainnet-readonly acceptance report was found. $RejectedText"
}
Write-Host "R5.1.6 prerequisite: PASS ($R516ReportPath)" -ForegroundColor Green

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Code = "P523"
$ArtifactRoot = Join-Path $ProjectRoot "artifacts\hedge_r523_install\$Stamp-$Code"
$BackupRoot = Join-Path $ProjectRoot "artifacts\hedge_r523_install_backups\r523-$Stamp-$Code"
New-Item -ItemType Directory -Path $ArtifactRoot -Force | Out-Null
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null

$InstallJsonl = Join-Path $ArtifactRoot "R5.2-INSTALL-MANIFEST.jsonl"
$InstallSummary = Join-Path $ArtifactRoot "R5.2-INSTALL-SUMMARY.json"
$InstalledCount = 0

foreach ($Entry in $Entries) {
    $Relative = [string]$Entry.path
    $RelativeWindows = $Relative -replace '/', '\'
    $SourcePath = Join-Path $SourceRoot $RelativeWindows
    $DestinationPath = Join-Path $ProjectRoot $RelativeWindows
    $DestinationDirectory = Split-Path -Parent $DestinationPath
    New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null

    $Existed = Test-Path -LiteralPath $DestinationPath -PathType Leaf
    if ($Existed) {
        $BackupPath = Join-Path $BackupRoot $RelativeWindows
        New-Item -ItemType Directory -Path (Split-Path -Parent $BackupPath) -Force | Out-Null
        Copy-Item -LiteralPath $DestinationPath -Destination $BackupPath -Force
    }

    Copy-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force
    $Actual = (Get-FileHash -LiteralPath $DestinationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne ([string]$Entry.sha256).ToLowerInvariant()) {
        throw "Installed file SHA256 mismatch: $Relative"
    }
    $Record = [ordered]@{
        path = $Relative
        sha256 = $Actual
        existed_before = [bool]$Existed
        installed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    ($Record | ConvertTo-Json -Compress) | Add-Content -LiteralPath $InstallJsonl -Encoding UTF8
    $InstalledCount += 1
}

$Summary = [ordered]@{
    schema_version = 1
    release = "R5.2.3"
    status = "INSTALLED"
    installed_files = $InstalledCount
    project_root = $ProjectRoot
    backup_root = $BackupRoot
    manifest_jsonl = $InstallJsonl
    manifest_sha256 = (Get-FileHash -LiteralPath $InstallJsonl -Algorithm SHA256).Hash.ToLowerInvariant()
    installer_exchange_access = "NONE"
    real_mainnet_order_requests_sent_by_installer = 0
    created_at_utc = [DateTime]::UtcNow.ToString("o")
}
$Summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $InstallSummary -Encoding UTF8
Write-Host "R5.2.3 cumulative source installation completed: $InstalledCount files" -ForegroundColor Green

$PreviousManifestEnv = [Environment]::GetEnvironmentVariable("R52_INSTALL_MANIFEST", "Process")
$OldPreference = $ErrorActionPreference
$DiagnosticCode = 2
try {
    [Environment]::SetEnvironmentVariable("R52_INSTALL_MANIFEST", $InstallJsonl, "Process")
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    & $Python `
        -m tools.hedge_r52.r52_toolkit.diagnostics `
        --project-root $ProjectRoot `
        --python $Python `
        --output-dir $ArtifactRoot
    if ($null -ne $LASTEXITCODE) {
        $DiagnosticCode = [int]$LASTEXITCODE
    }
}
catch {
    Write-Warning ("R5.2 diagnostic launcher failed: " + ($_ | Out-String))
    $DiagnosticCode = 2
}
finally {
    $ErrorActionPreference = $OldPreference
    [Environment]::SetEnvironmentVariable("R52_INSTALL_MANIFEST", $PreviousManifestEnv, "Process")
}

$DiagnosticSummary = Join-Path $ArtifactRoot "R5.2-DIAGNOSTIC-SUMMARY.json"
if (Test-Path -LiteralPath $DiagnosticSummary -PathType Leaf) {
    $Diagnostic = Get-Content -LiteralPath $DiagnosticSummary -Raw | ConvertFrom-Json
    Write-Host "R5.2.3 install diagnostics: $($Diagnostic.status)"
}
else {
    Write-Warning "R5.2 diagnostic summary was not generated."
    if ($DiagnosticCode -eq 0) {
        $DiagnosticCode = 2
    }
}

Write-Host "R5.2.3 diagnostic directory: $ArtifactRoot"
Write-Host "Installation backup: $BackupRoot"
Write-Host "Installer exchange access: NONE"
Write-Host "Real mainnet order requests sent by installer: 0"
Write-Host "All diagnostic sections were attempted even if an earlier section failed."

Set-Variable -Name LASTEXITCODE -Scope Global -Value $DiagnosticCode
