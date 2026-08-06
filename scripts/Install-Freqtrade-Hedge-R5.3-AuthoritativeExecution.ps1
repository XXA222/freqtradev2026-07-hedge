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
    throw "R5.3 source manifest was not found: $ManifestPath"
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($Manifest.schema_version -ne 1 -or $Manifest.release -ne "R5.3.0") {
    throw "R5.3 source manifest identity is invalid."
}
$Entries = @($Manifest.files)
if ($Entries.Count -lt 1) {
    throw "R5.3 source manifest is empty."
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
Write-Host "R5.3.0 source manifest: PASS ($($Entries.Count) files)" -ForegroundColor Green

function Find-LatestValidJsonReport {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Filter,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Validator,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "$Description artifact directory was not found: $Root"
    }
    $Candidates = @(
        Get-ChildItem `
            -LiteralPath $Root `
            -Filter $Filter `
            -File `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
    )
    if ($Candidates.Count -eq 0) {
        throw "$Description report was not found under: $Root"
    }
    $Rejected = New-Object System.Collections.Generic.List[string]
    foreach ($CandidateFile in $Candidates) {
        try {
            $Candidate = Get-Content -LiteralPath $CandidateFile.FullName -Raw | ConvertFrom-Json
            if (& $Validator $Candidate) {
                return [pscustomobject]@{
                    Path = $CandidateFile.FullName
                    Payload = $Candidate
                }
            }
            $Rejected.Add("$($CandidateFile.FullName): contract did not pass")
        }
        catch {
            $Rejected.Add("$($CandidateFile.FullName): $($_.Exception.Message)")
        }
    }
    throw "No valid $Description report was found. $($Rejected -join ' | ')"
}

$InstallPrerequisite = Find-LatestValidJsonReport `
    -Root (Join-Path $ProjectRoot "artifacts\hedge_r523_install") `
    -Filter "R5.2-DIAGNOSTIC-SUMMARY.json" `
    -Description "R5.2.3 install diagnostic" `
    -Validator {
        param($Value)
        return (
            $Value.status -eq "R5_2_FULL_DIAGNOSTICS_PASS" -and
            @($Value.failed_sections).Count -eq 0 -and
            [int64]$Value.real_mainnet_order_requests_sent_by_installer -eq 0 -and
            $Value.mainnet_live_exchange_write -eq "LOCKED"
        )
    }

$ShadowPrerequisite = Find-LatestValidJsonReport `
    -Root (Join-Path $ProjectRoot "artifacts\hedge_r523_mainnet_shadow") `
    -Filter "R5.2-MAINNET-SHADOW-REPORT.json" `
    -Description "R5.2.3 mainnet shadow" `
    -Validator {
        param($Value)
        return (
            $Value.status -eq "R5_2_PRODUCTION_STRATEGY_CORE_SHADOW_PASS" -and
            [int64]$Value.real_mainnet_order_requests_sent -eq 0 -and
            $Value.exchange_write -eq "LOCKED"
        )
    }

Write-Host "R5.2.3 install prerequisite: PASS ($($InstallPrerequisite.Path))" -ForegroundColor Green
Write-Host "R5.2.3 shadow prerequisite: PASS ($($ShadowPrerequisite.Path))" -ForegroundColor Green

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Code = "R530"
$ArtifactRoot = Join-Path $ProjectRoot "artifacts\hedge_r530_install\$Stamp-$Code"
$BackupRoot = Join-Path $ProjectRoot "artifacts\hedge_r530_install_backups\r530-$Stamp-$Code"
New-Item -ItemType Directory -Path $ArtifactRoot -Force | Out-Null
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null

$InstallJsonl = Join-Path $ArtifactRoot "R5.3-INSTALL-MANIFEST.jsonl"
$InstallSummary = Join-Path $ArtifactRoot "R5.3-INSTALL-SUMMARY.json"
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

# Keep the project-root delivery manifest current.  The manifest cannot list its own
# digest, so it is installed and audited as an explicit extra record.
$ProjectManifestPath = Join-Path $ProjectRoot "SOURCE-MANIFEST.json"
$ProjectManifestExisted = Test-Path -LiteralPath $ProjectManifestPath -PathType Leaf
if ($ProjectManifestExisted) {
    $ProjectManifestBackup = Join-Path $BackupRoot "SOURCE-MANIFEST.json"
    Copy-Item -LiteralPath $ProjectManifestPath -Destination $ProjectManifestBackup -Force
}
Copy-Item -LiteralPath $ManifestPath -Destination $ProjectManifestPath -Force
$ProjectManifestHash = (Get-FileHash -LiteralPath $ProjectManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$ManifestRecord = [ordered]@{
    path = "SOURCE-MANIFEST.json"
    sha256 = $ProjectManifestHash
    existed_before = [bool]$ProjectManifestExisted
    installed_at_utc = [DateTime]::UtcNow.ToString("o")
}
($ManifestRecord | ConvertTo-Json -Compress) | Add-Content -LiteralPath $InstallJsonl -Encoding UTF8
$InstalledCount += 1

$Summary = [ordered]@{
    schema_version = 1
    release = "R5.3.0"
    status = "INSTALLED"
    installed_files = $InstalledCount
    project_root = $ProjectRoot
    backup_root = $BackupRoot
    manifest_jsonl = $InstallJsonl
    manifest_sha256 = (Get-FileHash -LiteralPath $InstallJsonl -Algorithm SHA256).Hash.ToLowerInvariant()
    r523_install_prerequisite = $InstallPrerequisite.Path
    r523_shadow_prerequisite = $ShadowPrerequisite.Path
    installer_exchange_access = "NONE"
    real_mainnet_order_requests_sent_by_installer = 0
    created_at_utc = [DateTime]::UtcNow.ToString("o")
}
$Summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $InstallSummary -Encoding UTF8
Write-Host "R5.3.0 cumulative source installation completed: $InstalledCount files" -ForegroundColor Green

$PreviousManifestEnv = [Environment]::GetEnvironmentVariable("R53_INSTALL_MANIFEST", "Process")
$OldPreference = $ErrorActionPreference
$DiagnosticCode = 2
try {
    [Environment]::SetEnvironmentVariable("R53_INSTALL_MANIFEST", $InstallJsonl, "Process")
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    & $Python `
        -m tools.hedge_r53.r53_toolkit.diagnostics `
        --project-root $ProjectRoot `
        --python $Python `
        --output-dir $ArtifactRoot
    if ($null -ne $LASTEXITCODE) {
        $DiagnosticCode = [int]$LASTEXITCODE
    }
}
catch {
    Write-Warning ("R5.3 diagnostic launcher failed: " + ($_ | Out-String))
    $DiagnosticCode = 2
}
finally {
    $ErrorActionPreference = $OldPreference
    [Environment]::SetEnvironmentVariable("R53_INSTALL_MANIFEST", $PreviousManifestEnv, "Process")
}

$DiagnosticSummary = Join-Path $ArtifactRoot "R5.3-DIAGNOSTIC-SUMMARY.json"
if (Test-Path -LiteralPath $DiagnosticSummary -PathType Leaf) {
    $Diagnostic = Get-Content -LiteralPath $DiagnosticSummary -Raw | ConvertFrom-Json
    Write-Host "R5.3.0 install diagnostics: $($Diagnostic.status)"
}
else {
    Write-Warning "R5.3 diagnostic summary was not generated."
    if ($DiagnosticCode -eq 0) {
        $DiagnosticCode = 2
    }
}

Write-Host "R5.3.0 diagnostic directory: $ArtifactRoot"
Write-Host "Installation backup: $BackupRoot"
Write-Host "Installer exchange access: NONE"
Write-Host "Real mainnet order requests sent by installer: 0"
Write-Host "All diagnostic sections were attempted even if an earlier section failed."

Set-Variable -Name LASTEXITCODE -Scope Global -Value $DiagnosticCode
