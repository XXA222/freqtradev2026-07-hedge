[CmdletBinding()]
param(
    [string]$ProjectRoot = "D:\Program Files\freqtradev2026.07-hedge",
    [string]$ConfigPath = "",
    [string]$ContainerName = "freqtrade-hedge-r561-dryrun",
    [string]$UserDataPath = "/opt/freqtrade-hedge/user_data",
    [switch]$RunNetworkProbe,
    [switch]$RunDeterministicScenario
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$env:NO_COLOR = "1"

function Invoke-DockerCapture {
    param([string[]]$Arguments)
    $Output = & docker @Arguments 2>&1 | Out-String
    return [PSCustomObject]@{ ExitCode = $LASTEXITCODE; Output = $Output.Trim() }
}

function Get-DockerContainerState {
    param([string]$Name)
    $Result = Invoke-DockerCapture -Arguments @("inspect", $Name)
    if ($Result.ExitCode -eq 0) {
        $Inspect = $Result.Output | ConvertFrom-Json
        $Item = @($Inspect)[0]
        return [PSCustomObject]@{
            Outcome = "EXISTS"
            Status = [string]$Item.State.Status
            Running = [bool]$Item.State.Running
            Paused = [bool]$Item.State.Paused
            Restarting = [bool]$Item.State.Restarting
            Dead = [bool]$Item.State.Dead
            Error = ""
        }
    }
    if ($Result.Output.ToLowerInvariant().Contains("no such")) {
        return [PSCustomObject]@{
            Outcome = "MISSING"; Status = ""; Running = $false; Paused = $false
            Restarting = $false; Dead = $false; Error = $Result.Output
        }
    }
    return [PSCustomObject]@{
        Outcome = "QUERY_FAILED"; Status = ""; Running = $false; Paused = $false
        Restarting = $false; Dead = $false; Error = $Result.Output
    }
}

function Ensure-DockerContainerRunnable {
    param([string]$Name)
    $State = Get-DockerContainerState -Name $Name
    if ($State.Outcome -eq "QUERY_FAILED") {
        throw ("Unable to inspect Docker container {0}: {1}" -f $Name, $State.Error)
    }
    if ($State.Outcome -eq "MISSING") {
        throw ("Docker container does not exist: {0}" -f $Name)
    }
    if ($State.Dead) {
        throw ("Docker container is dead and must be recreated: {0}" -f $Name)
    }
    if ($State.Paused) {
        & docker unpause $Name | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ("Unable to unpause container: {0}" -f $Name) }
    }
    elseif (-not $State.Running -and $State.Status -in @("created", "exited")) {
        & docker start $Name | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ("Unable to start container: {0}" -f $Name) }
    }
    elseif ($State.Restarting) {
        throw ("Docker container is restarting; retry after it stabilizes: {0}" -f $Name)
    }
}

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw ("Project root not found: {0}" -f $ProjectRoot)
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $ProjectRoot "user_data\config_hedge_r56_dryrun_operations.json"
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw ("Config file not found: {0}" -f $ConfigPath)
}

# Real Windows PowerShell parser gate for every project-maintained PS1.
$ParseFailures = New-Object System.Collections.Generic.List[object]
Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "scripts") -Filter "*.ps1" -File | ForEach-Object {
    $Tokens = $null
    $Errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $_.FullName, [ref]$Tokens, [ref]$Errors
    )
    foreach ($ErrorItem in $Errors) {
        $ParseFailures.Add([PSCustomObject]@{
            File = $_.FullName; Message = $ErrorItem.Message
        })
    }
}
if ($ParseFailures.Count -gt 0) {
    $ParseFailures | Format-Table -AutoSize
    throw "PowerShell AST validation failed."
}

Ensure-DockerContainerRunnable -Name $ContainerName

$InspectRaw = & docker inspect $ContainerName | ConvertFrom-Json
$Container = @($InspectRaw)[0]
$ProxyEnv = @{}
foreach ($Entry in @($Container.Config.Env)) {
    $Parts = $Entry -split "=", 2
    if ($Parts.Count -eq 2 -and $Parts[0] -match "^(HTTP|HTTPS|ALL|NO)_PROXY$") {
        $ProxyEnv[$Parts[0]] = $Parts[1]
    }
}

$ConfigInContainer = "/tmp/r561-config.json"
Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 |
    docker exec -i $ContainerName /bin/sh -c "cat > $ConfigInContainer"
if ($LASTEXITCODE -ne 0) { throw "Unable to copy config into container for read-only validation." }

$PreflightScript = @'
import json
import pathlib
import sys
import freqtrade
from freqtrade.hedge.r561.config import validate_docker_dryrun_config
from freqtrade.hedge.r561.permissions import probe_directory

config_path = pathlib.Path("/tmp/r561-config.json")
config = json.loads(config_path.read_text(encoding="utf-8-sig"))
assert sys.version_info[:2] == (3, 12), sys.version
report = validate_docker_dryrun_config(config, run_upstream_schema=True)
permission = probe_directory("__USER_DATA__")
result = {
    "python": sys.version,
    "freqtrade_import": str(pathlib.Path(freqtrade.__file__).resolve()),
    "config_ready": report.ready,
    "config_errors": report.errors,
    "permission_ready": permission.ready,
    "permission_reasons": permission.reasons,
    "mainnet_writes": "LOCKED",
}
print(json.dumps(result, indent=2, default=str))
raise SystemExit(0 if report.ready and permission.ready else 2)
'@.Replace("__USER_DATA__", $UserDataPath.Replace("\", "\\").Replace('"', '\"'))

$PreflightScript | docker exec -i $ContainerName /opt/hedge-venv/bin/python -
if ($LASTEXITCODE -ne 0) { throw "Container Python/config/permission preflight failed." }

& docker exec $ContainerName /opt/hedge-venv/bin/python -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed inside the container." }

if ($RunNetworkProbe) {
    & docker exec $ContainerName /opt/hedge-venv/bin/python -m freqtrade.hedge.r561 `
        network-probe $ConfigInContainer --symbol "BTC/USDT:USDT" --attempts 20
    if ($LASTEXITCODE -ne 0) { throw "CCXT public network probe failed." }
}
if ($RunDeterministicScenario) {
    & docker exec $ContainerName /opt/hedge-venv/bin/python -m freqtrade.hedge.r561 `
        deterministic-scenario
    if ($LASTEXITCODE -ne 0) { throw "Deterministic Hedge paper scenario failed." }
}

Write-Host ""
Write-Host "R5.6.1 DOCKER PREFLIGHT: PASS" -ForegroundColor Green
Write-Host ("Container: {0}" -f $ContainerName)
Write-Host ("Proxy variables discovered: {0}" -f $ProxyEnv.Count)
Write-Host "Mainnet writes remain LOCKED." -ForegroundColor Yellow
