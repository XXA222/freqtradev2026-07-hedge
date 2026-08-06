[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
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
if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) {
    throw "Backup root was not found: $BackupRoot"
}

$Files = @(Get-ChildItem -LiteralPath $BackupRoot -File -Recurse)
if ($Files.Count -eq 0) {
    throw "Backup root contains no files."
}
foreach ($File in $Files) {
    $Relative = $File.FullName.Substring($BackupRoot.Length).TrimStart('\', '/')
    if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains("..")) {
        throw "Unsafe backup path: $Relative"
    }
    $Destination = Join-Path $ProjectRoot $Relative
    if ($PSCmdlet.ShouldProcess($Destination, "Restore R5.3 backup")) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Destination -Force
    }
}
Write-Host "R5.3 backup restore completed: $($Files.Count) files" -ForegroundColor Green
