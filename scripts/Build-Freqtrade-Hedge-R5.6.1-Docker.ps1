[CmdletBinding()]
param(
    [string]$ProjectRoot = "D:\Program Files\freqtradev2026.07-hedge",
    [string]$ImageName = "freqtrade-hedge:r561-dryrun",
    [string]$BaseAlias = "freqtrade-hedge-python-base:3.12",
    [switch]$InstallDevelop
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw ("Project root not found: {0}" -f $ProjectRoot)
}

$Candidates = @(
    "python:3.12.13-slim-bookworm",
    "python:3.12-slim-bookworm",
    "python:3.12-slim"
)
$Selected = $null
foreach ($Candidate in $Candidates) {
    docker image inspect $Candidate *> $null
    if ($LASTEXITCODE -eq 0) {
        $Selected = $Candidate
        break
    }
}
if ($null -eq $Selected) {
    throw @"
No locally verified Python 3.12 base image was found.
Load or pull one of these images using a trusted registry path, then rerun:
$($Candidates -join [Environment]::NewLine)
"@
}

docker tag $Selected $BaseAlias
if ($LASTEXITCODE -ne 0) { throw "Unable to create the verified local base-image alias." }
$BaseInspect = @(& docker image inspect $Selected | ConvertFrom-Json)[0]
if ($null -eq $BaseInspect) { throw "Unable to inspect the selected base image." }

$PythonVersionScript = @'
import sys
assert sys.version_info[:2] == (3, 12), sys.version
print(sys.version)
'@
$PythonVersionScript | docker run --rm -i $BaseAlias python -
if ($LASTEXITCODE -ne 0) { throw "Local base image failed the Python 3.12 ABI gate." }

$DevelopValue = if ($InstallDevelop) { "true" } else { "false" }
Push-Location $ProjectRoot
try {
    docker build `
        --pull=false `
        --build-arg ("PYTHON_BASE_IMAGE={0}" -f $BaseAlias) `
        --build-arg ("INSTALL_DEVELOP={0}" -f $DevelopValue) `
        --tag $ImageName `
        .
    if ($LASTEXITCODE -ne 0) { throw "Docker build failed." }
}
finally {
    Pop-Location
}

$Verify = @'
import pathlib
import sys
import freqtrade
assert sys.version_info[:2] == (3, 12), sys.version
root = pathlib.Path('/opt/freqtrade-hedge').resolve()
module = pathlib.Path(freqtrade.__file__).resolve()
assert root in module.parents, (root, module)
print('IMAGE_RUNTIME_GATE: PASS')
'@
$Verify | docker run --rm -i --entrypoint /opt/hedge-venv/bin/python $ImageName -
if ($LASTEXITCODE -ne 0) { throw "Built image failed the runtime verification gate." }

docker run --rm --entrypoint /opt/hedge-venv/bin/python $ImageName -m pip check
if ($LASTEXITCODE -ne 0) { throw "Built image failed pip check." }
$BuiltInspect = @(& docker image inspect $ImageName | ConvertFrom-Json)[0]
$ArtifactDirectory = Join-Path $ProjectRoot "artifacts\r561"
New-Item -ItemType Directory -Path $ArtifactDirectory -Force | Out-Null
$Metadata = [PSCustomObject]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    requested_base_alias = $BaseAlias
    selected_base_image = $Selected
    selected_base_id = [string]$BaseInspect.Id
    selected_base_repo_digests = @($BaseInspect.RepoDigests)
    built_image = $ImageName
    built_image_id = [string]$BuiltInspect.Id
    install_develop = [bool]$InstallDevelop
    pull = $false
}
$Metadata | ConvertTo-Json -Depth 20 |
    Set-Content -LiteralPath (Join-Path $ArtifactDirectory "docker-build-metadata.json") -Encoding UTF8

Write-Host ("R5.6.1 Docker image built: {0}" -f $ImageName) -ForegroundColor Green
Write-Host ("Verified base image: {0}" -f $Selected)
Write-Host ("Base image ID: {0}" -f $BaseInspect.Id)
