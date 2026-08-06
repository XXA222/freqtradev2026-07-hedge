[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string]$DeploymentConfig,

    [string]$TaskName = "Freqtrade-Hedge-R3.8"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$DeploymentConfig = [System.IO.Path]::GetFullPath($DeploymentConfig)
$StartScript = Join-Path $ProjectRoot "scripts\Start-Freqtrade-Hedge-R3.8.ps1"
if (-not (Test-Path -LiteralPath $StartScript -PathType Leaf)) {
    throw "R3.8 start script does not exist: $StartScript"
}
$Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -ProjectRoot `"$ProjectRoot`" -DeploymentConfig `"$DeploymentConfig`""
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
Write-Host "Registered scheduled task: $TaskName"
