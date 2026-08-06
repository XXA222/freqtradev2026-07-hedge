param([string]$ProjectRoot = "D:\Program Files\freqtrade-hedge")
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found: $Python" }
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Audit = Join-Path $ProjectRoot "user_data\audit\r2.2-local-debug-$Stamp"
New-Item -ItemType Directory -Path $Audit -Force | Out-Null
$Database = Join-Path $Audit "r2.2-smoke.sqlite"
$Output = Join-Path $Audit "r2.2-smoke-result.json"
$Log = Join-Path $Audit "r2.2-local-debug.log"
function Invoke-Checked([string]$Name, [string[]]$Arguments) {
    Write-Host "[$Name] $Python $($Arguments -join ' ')" -ForegroundColor Cyan
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Python @Arguments 2>&1 | Tee-Object -FilePath $Log -Append; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "$Name failed with exit code $code. Log: $Log" }
}
Push-Location -LiteralPath $ProjectRoot
try {
    Invoke-Checked "compileall" @("-m", "compileall", "-q", "freqtrade", "tests\hedge", "tools\hedge_local_smoke_r22.py")
    Invoke-Checked "r22_deep_regressions" @("-m", "pytest", "-q", "--tb=short", "--timeout=120", "-o", "addopts=", "tests\hedge\integration\test_deep_runtime_hardening_r22.py", "tests\hedge\integration\test_local_backtest_paper_r21.py", "tests\hedge\integration\test_paper_dataprovider_events_r20.py", "tests\hedge\execution\test_ledger_fake_account_v14.py", "tests\hedge\persistence\test_execution_sql_authority_r20.py")
    Invoke-Checked "offline_durable_smoke" @("tools\hedge_local_smoke_r22.py", "--database", $Database, "--output", $Output)
} finally {
    Pop-Location
}
$result = Get-Content -LiteralPath $Output -Raw | ConvertFrom-Json
if ($result.status -ne "PASS") { throw "R2.2 offline durable smoke did not report PASS" }
if ([int]$result.second_fills -lt 1) { throw "R2.2 smoke did not execute a next-candle fill" }
Write-Host "`n========== R2.2 LOCAL DEBUG COMPLETE ==========" -ForegroundColor Green
Write-Host "Status: PASS" -ForegroundColor Green
Write-Host "Audit: $Audit" -ForegroundColor Cyan
Write-Host "Result: $Output" -ForegroundColor Cyan
Write-Host "Exchange writes: LOCKED" -ForegroundColor Yellow
