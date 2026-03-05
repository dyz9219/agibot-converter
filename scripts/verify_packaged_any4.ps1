param(
    [string]$DistRoot = "dist/AgibotConverterShell",
    [string]$RepoAny4File = "any4lerobot/agibot2lerobot/agibot_h5.py"
)

$ErrorActionPreference = "Stop"

$repoPath = (Resolve-Path $RepoAny4File).Path
$distPath = Join-Path (Resolve-Path $DistRoot).Path "_internal\any4lerobot\agibot2lerobot\agibot_h5.py"

if (-not (Test-Path $repoPath)) {
    throw "Repo any4 file not found: $RepoAny4File"
}
if (-not (Test-Path $distPath)) {
    throw "Packaged any4 file not found: $distPath"
}

$repoHash = (Get-FileHash $repoPath -Algorithm SHA256).Hash
$distHash = (Get-FileHash $distPath -Algorithm SHA256).Hash

if ($repoHash -ne $distHash) {
    throw "Hash mismatch between repo and packaged any4 file. repo=$repoHash packaged=$distHash"
}

Write-Host "OK: packaged any4 matches repo source."
Write-Host "repo=$repoPath"
Write-Host "packaged=$distPath"
Write-Host "sha256=$repoHash"
