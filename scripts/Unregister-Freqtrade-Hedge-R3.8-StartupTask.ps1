[CmdletBinding()]
param(
    [string]$TaskName = "Freqtrade-Hedge-R3.8"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Write-Host "Scheduled task absent: $TaskName"
