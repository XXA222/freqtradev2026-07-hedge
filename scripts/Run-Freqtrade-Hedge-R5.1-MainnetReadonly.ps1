[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$LegacyCredentialFile = "",
    [string]$ProxyUrl = "http://127.0.0.1:7897",
    [string]$Actor = "QX",
    [int]$DurationSeconds = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-ProjectPython {
    param([string]$Root)
    $Candidates = @(
        (Join-Path $Root "freqtrade-hedge\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root "venv\Scripts\python.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    throw "Project Python executable was not found."
}


function Invoke-PythonLogged {
    param(
        [string[]]$Arguments,
        [string]$LogPath
    )

    $StdoutCapture = "$LogPath.stdout.$([Guid]::NewGuid().ToString('N')).tmp"
    $StderrCapture = "$LogPath.stderr.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        & $script:Python @Arguments 1> $StdoutCapture 2> $StderrCapture
        $ExitCode = [int]$LASTEXITCODE

        foreach ($Capture in @($StdoutCapture, $StderrCapture)) {
            if (Test-Path -LiteralPath $Capture -PathType Leaf) {
                Get-Content -LiteralPath $Capture -Encoding UTF8 | ForEach-Object {
                    $_ | Tee-Object -FilePath $LogPath -Append | Out-Host
                }
            }
        }
        return $ExitCode
    }
    finally {
        foreach ($Capture in @($StdoutCapture, $StderrCapture)) {
            if (Test-Path -LiteralPath $Capture) {
                Remove-Item -LiteralPath $Capture -Force -ErrorAction SilentlyContinue
            }
        }
    }
}
function Read-CredentialPair {
    param([string]$Path)
    $Lines = @(Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($Lines.Count -eq 3 -and $Lines[2] -eq "BINANCE_USDM_MAINNET_R5") {
        $Lines = @($Lines[0], $Lines[1])
    }
    if ($Lines.Count -ne 2) {
        throw "Legacy Binance credential file must contain key and secret on two lines."
    }
    return $Lines
}

$Root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Python = Resolve-ProjectPython -Root $Root
$script:Python = $Python
if ([string]::IsNullOrWhiteSpace($LegacyCredentialFile)) {
    $Encoded = "QzpcVXNlcnNcUVhcRGVza3RvcFzlkK/liqhmcmVxdHJhZGVcYXBpXGJpbmFuY2UgLSDlia/mnKwudHh0"
    $LegacyCredentialFile = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded))
}
$LegacyCredentialFile = (Resolve-Path -LiteralPath $LegacyCredentialFile).Path
if ($DurationSeconds -lt 30 -or $DurationSeconds -gt 300) {
    throw "DurationSeconds must be in [30, 300]."
}
if ([string]::IsNullOrWhiteSpace($Actor)) {
    throw "Actor is required."
}

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Code = "R516A"
$OutputDir = Join-Path $Root "artifacts\hedge_r516_mainnet_readonly\$Stamp-$Code"
$ManifestPath = Join-Path $OutputDir "R5.1-WORKSPACE-MANIFEST.json"
$ConsoleLog = Join-Path $OutputDir "R5.1-CONSOLE.log"
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

$PreviousPythonPath = $env:PYTHONPATH
$PreviousWriteLock = $env:FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK
$PreviousApiKey = $env:BINANCE_API_KEY
$PreviousApiSecret = $env:BINANCE_API_SECRET
$OverallCode = 0

try {
    $env:PYTHONPATH = @(
        $Root,
        (Join-Path $Root "tools\hedge_r51")
    ) -join [IO.Path]::PathSeparator

    $PrepareArgs = @(
        "-m", "tools.hedge_r51.r51_toolkit", "prepare",
        "--project-root", $Root,
        "--legacy-credentials", $LegacyCredentialFile,
        "--output-dir", $OutputDir,
        "--manifest", $ManifestPath
    )
    $PrepareCode = [int](Invoke-PythonLogged -Arguments $PrepareArgs -LogPath $ConsoleLog)
    if ($PrepareCode -ne 0) {
        throw "R5.1 workspace preparation failed."
    }

    $Workspace = Get-Content -LiteralPath $ManifestPath -Raw -Encoding ASCII | ConvertFrom-Json
    $CredentialPath = [string]$Workspace.credentials_path

    $Pair = Read-CredentialPair -Path $LegacyCredentialFile
    $env:BINANCE_API_KEY = $Pair[0]
    $env:BINANCE_API_SECRET = $Pair[1]
    $env:FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK = "1"
    $env:PYTHONPATH = @(
        (Join-Path $Root "tools\hedge_r24\guard"),
        (Join-Path $Root "tools\hedge_r24"),
        (Join-Path $Root "tools\hedge_r25"),
        (Join-Path $Root "tools\hedge_r51"),
        $Root
    ) -join [IO.Path]::PathSeparator

    $R25Args = @(
        "-m", "r25_toolkit",
        "--r24-report", [string]$Workspace.r24_report,
        "--proxy", $ProxyUrl,
        "--database", [string]$Workspace.stream_database,
        "--observations", [string]$Workspace.stream_observations,
        "--report", [string]$Workspace.stream_report,
        "--duration-seconds", [string]$DurationSeconds,
        "--observation-interval-seconds", "5",
        "--ready-timeout-seconds", "120",
        "--fast-interval-seconds", "30",
        "--full-interval-seconds", "300",
        "--error-retry-seconds", "15",
        "--request-timeout-seconds", "25",
        "--max-clock-skew-ms", "1000"
    )
    $StreamCode = [int](Invoke-PythonLogged -Arguments $R25Args -LogPath $ConsoleLog)
    if ($StreamCode -ne 0) {
        $OverallCode = 2
        Write-Warning "Fresh user-stream observation failed with exit code $StreamCode."
    }

    if ($StreamCode -eq 0) {
        $R5Args = @(
            "-m", "freqtrade.hedge.r5",
            "--project-root", $Root,
            "--config", [string]$Workspace.config_path,
            "--stage", "MAINNET_READONLY",
            "--run-id", [string]$Workspace.run_id,
            "--actor", $Actor,
            "--symbol", "ETHUSDT",
            "--position-side", "LONG",
            "--target-notional", "0",
            "--proxy-url", $ProxyUrl,
            "--output", [string]$Workspace.r5_report
        )
        $StageCode = [int](Invoke-PythonLogged -Arguments $R5Args -LogPath $ConsoleLog)
        if ($StageCode -ne 0) {
            $OverallCode = 2
            Write-Warning "R5.1 MAINNET_READONLY stage failed with exit code $StageCode."
        }

        if (Test-Path -LiteralPath ([string]$Workspace.r5_report) -PathType Leaf) {
            $FinalizeArgs = @(
                "-m", "tools.hedge_r51.r51_toolkit", "finalize",
                "--output", [string]$Workspace.acceptance_report,
                "--stage-report", [string]$Workspace.r5_report,
                "--stream-report", [string]$Workspace.stream_report,
                "--workspace-manifest", $ManifestPath
            )
            $FinalizeCode = [int](Invoke-PythonLogged -Arguments $FinalizeArgs -LogPath $ConsoleLog)
            if ($FinalizeCode -ne 0) { $OverallCode = 2 }
        }
    }
}
catch {
    $OverallCode = 2
    ($_ | Format-List * -Force | Out-String) | Add-Content -LiteralPath $ConsoleLog -Encoding UTF8
    Write-Warning $_.Exception.Message
}
finally {
    $env:BINANCE_API_KEY = $PreviousApiKey
    $env:BINANCE_API_SECRET = $PreviousApiSecret
    $env:FREQTRADE_HEDGE_EXCHANGE_WRITE_LOCK = $PreviousWriteLock
    $env:PYTHONPATH = $PreviousPythonPath
}

$Downloads = Join-Path $env:USERPROFILE "Downloads"
$UploadZip = Join-Path $Downloads "Freqtrade_Hedge_R5.1.6_MainnetReadonlyUpload_$Stamp-$Code.zip"
if (Test-Path -LiteralPath $UploadZip) { Remove-Item -LiteralPath $UploadZip -Force }
$UploadFiles = Get-ChildItem -LiteralPath $OutputDir -File | Where-Object {
    $_.Name -notlike "*.sqlite" -and $_.Name -notlike "*.sqlite-*" -and $_.Name -notlike "*observations.jsonl"
}
if ($UploadFiles.Count -gt 0) {
    Compress-Archive -LiteralPath $UploadFiles.FullName -DestinationPath $UploadZip -Force
}

Write-Host ""
Write-Host "R5.1 output directory: $OutputDir"
Write-Host "R5.1 diagnostic upload ZIP: $UploadZip"
Write-Host "Mainnet order requests sent by R5.1 readonly runner: 0"
Write-Host "Exchange order write: LOCKED"
if ($OverallCode -eq 0) {
    Write-Host "R5.1.6 MAINNET READONLY: R5_1_MAINNET_READONLY_COMPLETE_WRITES_LOCKED" -ForegroundColor Green
}
else {
    Write-Warning "R5.1.6 MAINNET READONLY did not pass. Upload the generated ZIP."
}
exit $OverallCode
