[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$BackupRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$BackupRoot = [System.IO.Path]::GetFullPath($BackupRoot)
$CreatedList = Join-Path $BackupRoot "created-files.txt"
$ReplacedList = Join-Path $BackupRoot "replaced-files.txt"
if (-not (Test-Path -LiteralPath $CreatedList -PathType Leaf)) {
    throw "Backup created-files list was not found."
}
if (-not (Test-Path -LiteralPath $ReplacedList -PathType Leaf)) {
    throw "Backup replaced-files list was not found."
}
$Created = @(Get-Content -LiteralPath $CreatedList | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
[array]::Reverse($Created)
foreach ($RelativePath in $Created) {
    $Destination = Join-Path $ProjectRoot $RelativePath
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Remove-Item -LiteralPath $Destination -Force
    }
}
foreach ($RelativePath in @(Get-Content -LiteralPath $ReplacedList | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
    $Source = Join-Path $BackupRoot $RelativePath
    $Destination = Join-Path $ProjectRoot $RelativePath
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Backup file was not found: $RelativePath"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}
Set-Content -LiteralPath (Join-Path $BackupRoot "MANUAL_RESTORE_COMPLETE") -Value "MANUAL_RESTORE_COMPLETE" -Encoding Ascii
Write-Host "R4 backup restore completed: $BackupRoot"
