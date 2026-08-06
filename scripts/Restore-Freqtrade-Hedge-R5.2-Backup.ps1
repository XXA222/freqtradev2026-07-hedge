[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,

    [Parameter(Mandatory = $true)]
    [string]$InstallManifestJsonl
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$BackupRoot = [System.IO.Path]::GetFullPath($BackupRoot)
if (-not (Test-Path -LiteralPath $InstallManifestJsonl -PathType Leaf)) {
    throw "Install manifest JSONL was not found."
}
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) {
    throw "Backup root was not found."
}

$Count = 0
Get-Content -LiteralPath $InstallManifestJsonl | ForEach-Object {
    if ([string]::IsNullOrWhiteSpace($_)) {
        return
    }
    $Record = $_ | ConvertFrom-Json
    $Relative = [string]$Record.path
    if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains("..")) {
        throw "Unsafe restore path: $Relative"
    }
    $RelativeWindows = $Relative -replace '/', '\'
    $Destination = Join-Path $ProjectRoot $RelativeWindows
    if ([bool]$Record.existed_before) {
        $Backup = Join-Path $BackupRoot $RelativeWindows
        if (-not (Test-Path -LiteralPath $Backup -PathType Leaf)) {
            throw "Backup file is missing: $Relative"
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
        Copy-Item -LiteralPath $Backup -Destination $Destination -Force
    }
    elseif (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Remove-Item -LiteralPath $Destination -Force
    }
    $Count += 1
}
Write-Host "R5.2 restore completed: $Count files" -ForegroundColor Green
