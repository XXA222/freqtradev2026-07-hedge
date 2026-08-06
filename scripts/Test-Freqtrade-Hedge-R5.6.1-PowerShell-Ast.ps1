[CmdletBinding()]
param(
    [string]$ProjectRoot = "D:\Program Files\freqtradev2026.07-hedge",
    [string]$OutputPath = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw ("Project root not found: {0}" -f $ProjectRoot)
}
$Files = @(
    Get-ChildItem -LiteralPath $ProjectRoot -Recurse -Filter "*.ps1" -File |
        Where-Object {
            $_.FullName -notmatch "[\\/](artifacts|freqtrade-hedge|build|user_data)[\\/]"
        }
)
$ParseErrorRows = @()
foreach ($File in $Files) {
    $Tokens = $null
    $ParseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $File.FullName,
        [ref]$Tokens,
        [ref]$ParseErrors
    )
    foreach ($Item in $ParseErrors) {
        $ParseErrorRows += [PSCustomObject]@{
            file = $File.FullName.Substring($ProjectRoot.Length).TrimStart('\')
            message = $Item.Message
            error_id = $Item.ErrorId
            text = [string]$Item.Extent.Text
        }
    }
}
$Result = [PSCustomObject]@{
    status = if ($ParseErrorRows.Count -eq 0) { "PASS" } else { "FAIL" }
    powershell_version = $PSVersionTable.PSVersion.ToString()
    files_checked = $Files.Count
    error_count = $ParseErrorRows.Count
    errors = $ParseErrorRows
}
$Json = $Result | ConvertTo-Json -Depth 20
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $Parent = Split-Path -Parent $OutputPath
    if (-not [string]::IsNullOrWhiteSpace($Parent)) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }
    $Json | Set-Content -LiteralPath $OutputPath -Encoding UTF8
}
$Json
if ($ParseErrorRows.Count -ne 0) { exit 2 }
exit 0
