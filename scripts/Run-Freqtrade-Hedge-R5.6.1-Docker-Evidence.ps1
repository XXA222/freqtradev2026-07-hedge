[CmdletBinding()]
param(
    [string]$ContainerName = "freqtrade-hedge-r561-dryrun",
    [string]$OutputDirectory = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $env:USERPROFILE "Downloads"
}
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Work = Join-Path $env:TEMP ("FT-Hedge-R561-Evidence-{0}" -f $Timestamp)
New-Item -ItemType Directory -Path $Work -Force | Out-Null
try {
    $Inspect = @(& docker inspect $ContainerName | ConvertFrom-Json)[0]
    if ($null -eq $Inspect) { throw ("Unable to inspect container: {0}" -f $ContainerName) }
    $SanitizedEnv = @()
    foreach ($Entry in @($Inspect.Config.Env)) {
        $Parts = [string]$Entry -split "=", 2
        if ($Parts.Count -eq 2) {
            $Name = $Parts[0]
            if ($Name -match "(?i)(PROXY|SECRET|TOKEN|PASSWORD|KEY|CREDENTIAL)") {
                $SanitizedEnv += ("{0}=***REDACTED***" -f $Name)
            }
            else {
                $SanitizedEnv += $Entry
            }
        }
    }
    $Inspect.Config.Env = $SanitizedEnv
    $Inspect | ConvertTo-Json -Depth 100 |
        Set-Content -LiteralPath (Join-Path $Work "docker-inspect-redacted.json") -Encoding UTF8

    $LogText = (& docker logs --tail 1000 $ContainerName 2>&1 | Out-String)
    $LogText = $LogText -replace '(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+', '$1=***REDACTED***'
    $LogText = $LogText -replace '(?i)(https?://)[^/@\s:]+:[^/@\s]+@', '$1***:***@'
    $LogText | Set-Content -LiteralPath (Join-Path $Work "container-log-tail-redacted.txt") -Encoding UTF8

    & docker exec $ContainerName /opt/hedge-venv/bin/python -m pip freeze |
        Set-Content -LiteralPath (Join-Path $Work "pip-freeze.txt") -Encoding UTF8
    & docker exec $ContainerName /opt/hedge-venv/bin/python -m pip check 2>&1 |
        Set-Content -LiteralPath (Join-Path $Work "pip-check.txt") -Encoding UTF8

    $State = [PSCustomObject]@{
        generated_at = (Get-Date).ToUniversalTime().ToString("o")
        container = $ContainerName
        image = [string]$Inspect.Config.Image
        image_id = [string]$Inspect.Image
        status = [string]$Inspect.State.Status
        running = [bool]$Inspect.State.Running
        paused = [bool]$Inspect.State.Paused
        restart_count = [int]$Inspect.RestartCount
        mainnet_writes = "LOCKED"
    }
    $State | ConvertTo-Json -Depth 10 |
        Set-Content -LiteralPath (Join-Path $Work "runtime-state.json") -Encoding UTF8

    $Zip = Join-Path $OutputDirectory ("FT_Hedge_R5.6.1_Docker_Evidence_{0}.zip" -f $Timestamp)
    Compress-Archive -Path (Join-Path $Work "*") -DestinationPath $Zip -Force
    $Hash = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host ("Evidence ZIP: {0}" -f $Zip) -ForegroundColor Green
    Write-Host ("SHA256: {0}" -f $Hash)
}
finally {
    Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
}
