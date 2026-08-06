[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ContainerName,
    [Parameter(Mandatory = $true)][string]$ImageName
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Inspect = & docker inspect $ContainerName 2>&1
if ($LASTEXITCODE -ne 0) {
    throw ("Unable to inspect container {0}: {1}" -f $ContainerName, ($Inspect | Out-String))
}
$State = @($Inspect | ConvertFrom-Json)[0].State
if ([bool]$State.Paused) {
    & docker unpause $ContainerName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ("Unable to unpause container: {0}" -f $ContainerName) }
}
try {
    & docker commit --pause=false $ContainerName $ImageName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker commit failed" }
}
finally {
    $After = & docker inspect $ContainerName 2>$null | ConvertFrom-Json
    if ($null -ne $After -and [bool]@($After)[0].State.Paused) {
        & docker unpause $ContainerName | Out-Null
    }
}
Write-Host ("Committed image without pausing source container: {0}" -f $ImageName) -ForegroundColor Green
