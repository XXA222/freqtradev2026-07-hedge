param(
    [Parameter(Mandatory = $true)]
    [string]$TokenFile
)

$ErrorActionPreference = "Stop"
$Path = (Resolve-Path -LiteralPath $TokenFile).Path
$Lines = Get-Content -LiteralPath $Path
if ($Lines.Count -ne 1 -or [string]::IsNullOrWhiteSpace($Lines[0])) {
    throw "Token file must contain exactly one non-empty line."
}
$Bytes = [System.Text.Encoding]::UTF8.GetBytes($Lines[0].Trim())
$Sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $Hash = $Sha.ComputeHash($Bytes)
} finally {
    $Sha.Dispose()
}
([System.BitConverter]::ToString($Hash)).Replace("-", "").ToLowerInvariant()
