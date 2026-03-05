param(
    [string]$ExePath
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "Missing python venv: $py"
}

if (-not $ExePath) {
    $fast = Join-Path $root "dist\AgibotConverterShell-fast\AgibotConverterShell-fast.exe"
    $full = Join-Path $root "dist\AgibotConverterShell-full\AgibotConverterShell-full.exe"
    if (Test-Path $full) {
        $ExePath = $full
    } elseif (Test-Path $fast) {
        $ExePath = $fast
    } else {
        throw "No packaged EXE found. Please pass -ExePath."
    }
}

if (-not (Test-Path $ExePath)) {
    throw "EXE not found: $ExePath"
}

$fingerprintTempScript = Join-Path $env:TEMP "fingerprint_$([guid]::NewGuid().ToString()).py"
$fingerprintCode = @"
import hashlib
from pathlib import Path

root = Path(r"$($root.Replace('\', '/'))")
targets = [root / "src" / "agibot_converter", root / "scripts" / "build_exe.ps1", root / "AgibotConverterShell.spec"]
h = hashlib.sha256()
for t in targets:
    if t.is_dir():
        for p in sorted([x for x in t.rglob("*") if x.is_file()]):
            if "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"}:
                continue
            rel = p.relative_to(root).as_posix().encode("utf-8")
            h.update(rel)
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    elif t.is_file():
        rel = t.relative_to(root).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\0")
        h.update(t.read_bytes())
        h.update(b"\0")
print(h.hexdigest())
"@
[System.IO.File]::WriteAllText($fingerprintTempScript, $fingerprintCode, [System.Text.UTF8Encoding]::new($false))

$expectedCommit = (git rev-parse HEAD).Trim()
$expectedFingerprint = (& $py $fingerprintTempScript).Trim()
Remove-Item $fingerprintTempScript -Force -ErrorAction SilentlyContinue

$buildLogDir = Join-Path $root "build"
New-Item -ItemType Directory -Path $buildLogDir -Force | Out-Null
$stdoutPath = Join-Path $buildLogDir "verify-build-info.stdout.txt"
$stderrPath = Join-Path $buildLogDir "verify-build-info.stderr.txt"

$proc = Start-Process -FilePath $ExePath -ArgumentList @("--internal-build-info") -Wait -NoNewWindow -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
if ($proc.ExitCode -ne 0) {
    $stderr = if (Test-Path $stderrPath) { Get-Content $stderrPath -Raw } else { "" }
    throw "Failed to read packaged build info, exit=$($proc.ExitCode)`n$stderr"
}

$raw = Get-Content $stdoutPath -Raw
$actual = $raw | ConvertFrom-Json

$problems = @()
if ($actual.git_commit -ne $expectedCommit) {
    $problems += "git_commit mismatch: packaged=$($actual.git_commit), repo=$expectedCommit"
}
if ($actual.source_fingerprint -ne $expectedFingerprint) {
    $problems += "source_fingerprint mismatch: packaged=$($actual.source_fingerprint), repo=$expectedFingerprint"
}
if (-not $actual.profile) {
    $problems += "missing packaged profile field"
}

if ($problems.Count -gt 0) {
    Write-Host "FINGERPRINT_CHECK_FAIL" -ForegroundColor Red
    $problems | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
    exit 1
}

Write-Host "FINGERPRINT_CHECK_OK" -ForegroundColor Green
Write-Host "profile=$($actual.profile)"
Write-Host "git_commit=$($actual.git_commit)"
Write-Host "source_fingerprint=$($actual.source_fingerprint)"
