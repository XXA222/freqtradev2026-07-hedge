[CmdletBinding()]
param(
    [string]$ProjectRoot = "D:\Program Files\freqtrade-hedge",
    [string]$OutputRoot = "D:\Program Files\freqtrade-hedge-backups\checkpoints"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $Parent = Split-Path -Parent $Path
    if ($Parent) { New-Item -ItemType Directory -Force -Path $Parent | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Project root does not exist: $ProjectRoot"
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required."
}

Set-Location -LiteralPath $ProjectRoot
if ((git rev-parse --is-inside-work-tree).Trim() -ne "true") {
    throw "Project root is not a Git worktree."
}

$Status = @(git -c core.quotepath=false status --porcelain=v1 --untracked-files=all)
if ($Status.Count -ne 0) {
    $Status | ForEach-Object { Write-Host $_ }
    throw "The worktree is not clean. Review, commit, or remove every change before checkpointing."
}

git diff --check
if ($LASTEXITCODE -ne 0) { throw "git diff --check failed." }

$SensitiveTracked = @(
    git -c core.quotepath=false ls-files |
        Where-Object {
            $_ -match '(^|/)\.env($|\.)' -or
            $_ -match '\.(pem|key|p12|pfx|kdbx|sqlite|sqlite3|db)$' -or
            $_ -match '(^|/)(secrets?|credentials?)(/|$)' -or
            $_ -match '(^|/)user_data/(audit|integration|logs|data)(/|$)'
        }
)
if ($SensitiveTracked.Count -ne 0) {
    $SensitiveTracked | ForEach-Object { Write-Host $_ }
    throw "Potentially sensitive/runtime files are tracked. Remove them before checkpointing."
}

$Branch = (git branch --show-current).Trim()
$Commit = (git rev-parse HEAD).Trim()
$Short = (git rev-parse --short=12 HEAD).Trim()
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$SafeBranch = ($Branch -replace '[^A-Za-z0-9._-]', '-')
$Name = "freqtrade-hedge-checkpoint-$SafeBranch-$Short-$Timestamp"
$Directory = Join-Path $OutputRoot $Name
$SourceZip = Join-Path $Directory "$Name-source.zip"
$Bundle = Join-Path $Directory "$Name-all-refs.bundle"
$Info = Join-Path $Directory "CHECKPOINT_INFO.txt"
$Hashes = Join-Path $Directory "SHA256SUMS.txt"

New-Item -ItemType Directory -Force -Path $Directory | Out-Null

git archive --format=zip --prefix="$Name/" --output=$SourceZip HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive failed." }

git bundle create $Bundle --all
if ($LASTEXITCODE -ne 0) { throw "git bundle create failed." }
git bundle verify $Bundle | Out-Null
if ($LASTEXITCODE -ne 0) { throw "git bundle verify failed." }

$Tags = @(git tag --points-at HEAD)
$Remote = @(git remote -v)
$InfoText = @(
    "Project=freqtrade-hedge"
    "CreatedAt=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
    "Branch=$Branch"
    "Commit=$Commit"
    "TagsAtHead=$($Tags -join ',')"
    "WorktreeClean=True"
    "SourceArchive=$([IO.Path]::GetFileName($SourceZip))"
    "GitBundle=$([IO.Path]::GetFileName($Bundle))"
    ""
    "Remotes:"
    $Remote
    ""
) -join "`n"
Write-Utf8NoBom -Path $Info -Content $InfoText

$HashLines = foreach ($Path in @($SourceZip, $Bundle, $Info)) {
    $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $([IO.Path]::GetFileName($Path))"
}
Write-Utf8NoBom -Path $Hashes -Content (($HashLines -join "`n") + "`n")

Write-Host "Checkpoint created:" -ForegroundColor Green
Write-Host $Directory
Write-Host "Commit: $Commit"
Write-Host "Source ZIP: $SourceZip"
Write-Host "Git bundle: $Bundle"
Write-Host "Hashes: $Hashes"
explorer.exe $Directory
