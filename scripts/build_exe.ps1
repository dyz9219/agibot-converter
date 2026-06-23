param(
    [ValidateSet("fast", "full")]
    [string]$Profile,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
$any4Local = Join-Path $root "any4lerobot"
$any4Parent = Join-Path (Split-Path $root -Parent) "any4lerobot"
$any4 = if (Test-Path (Join-Path $any4Local "agibot2lerobot\agibot_h5.py")) { $any4Local } else { $any4Parent }
$assets = Join-Path $root "assets"

if (-not (Test-Path $py)) {
    python -m venv $venv
}

if ($Clean) {
    if (Test-Path "$root\build") { Remove-Item -Recurse -Force "$root\build" }
    if (Test-Path "$root\dist") { Remove-Item -Recurse -Force "$root\dist" }
    if (Test-Path "$root\build-onefile") { Remove-Item -Recurse -Force "$root\build-onefile" }
    if (Test-Path "$root\dist-onefile") { Remove-Item -Recurse -Force "$root\dist-onefile" }
}

if (-not $Profile) {
    throw "Missing -Profile. You must pass -Profile fast or -Profile full."
}
$resolvedProfile = $Profile.ToLowerInvariant()

& (Join-Path $PSScriptRoot "install_curated_env.ps1") -IncludeDev

if (-not (Test-Path (Join-Path $any4 "agibot2lerobot\agibot_h5.py"))) {
    throw "Missing bundled any4lerobot source: $any4"
}
if (-not (Test-Path (Join-Path $assets "pku_logo.png"))) {
    throw "Missing app icon asset: $assets\pku_logo.png"
}
if (-not (Test-Path (Join-Path $assets "pku_logo.ico"))) {
    throw "Missing app icon asset: $assets\pku_logo.ico"
}

$commit = (git rev-parse HEAD).Trim()
$dirtyLines = @(git status --porcelain)
$isDirty = $dirtyLines.Count -gt 0
$dirtyPreview = @($dirtyLines | Select-Object -First 200)
$fingerprintTempScript = Join-Path $env:TEMP "fingerprint_$([guid]::NewGuid().ToString()).py"
$fingerprintCode = @"
import hashlib
from pathlib import Path

root = Path(r"$($root.Replace('\', '/'))")
targets = [root / "src" / "data_converter", root / "scripts" / "build_exe.ps1", root / "DataConverterShell.spec"]
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
$sourceFingerprint = (& $py $fingerprintTempScript).Trim()
Remove-Item $fingerprintTempScript -Force -ErrorAction SilentlyContinue

$metaDir = Join-Path $root "build\build-meta"
New-Item -ItemType Directory -Path $metaDir -Force | Out-Null
$metaPath = Join-Path $metaDir "build_meta.json"
$meta = [ordered]@{
    profile = $resolvedProfile
    build_time = (Get-Date).ToString("o")
    git_commit = $commit
    git_dirty = $isDirty
    source_fingerprint = $sourceFingerprint
    dirty_files = $dirtyPreview
}
$metaJson = $meta | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($metaPath, $metaJson, [System.Text.UTF8Encoding]::new($false))

if ($resolvedProfile -eq "fast") {
    $distPath = "$root\dist"
    $workPath = "$root\build\DataConverterShell-fast"
    $name = "DataConverterShell-fast"
    $collectArgs = @(
        "--collect-all", "flet",
        "--collect-all", "flet_desktop",
        "--collect-all", "psutil",
        "--hidden-import", "psutil._psutil_windows",
        "--collect-data", "tkinter"
    )
} else {
    $distPath = "$root\dist"
    $workPath = "$root\build\DataConverterShell-full"
    $name = "DataConverterShell-full"
    $collectArgs = @(
        "--collect-all", "flet",
        "--collect-all", "flet_desktop",
        "--collect-submodules", "ray",
        "--collect-binaries", "ray",
        "--collect-binaries", "torch",
        "--hidden-import", "torch",
        "--collect-all", "lerobot",
        "--collect-all", "jsonlines",
        "--collect-all", "agibot_utils",
        "--collect-all", "psutil",
        "--hidden-import", "psutil._psutil_windows",
        "--hidden-import", "ray.thirdparty_files.psutil._psutil_windows",
        "--collect-all", "ray.thirdparty_files.psutil",
        "--collect-all", "rosbags",
        "--collect-submodules", "rosbags.typesys.stores",
        "--collect-data", "tkinter"
    )
}

Write-Host "Build profile: $resolvedProfile" -ForegroundColor Cyan
Write-Host "Dist path: $distPath" -ForegroundColor Cyan

$baseArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--windowed",
    "--name", $name,
    "--distpath", $distPath,
    "--workpath", $workPath,
    "--icon", "$assets\pku_logo.ico",
    "--add-data", "$any4;any4lerobot",
    "--add-data", "$assets;assets",
    "--add-data", "$metaPath;assets",
    "src/data_converter/main.py"
)

& $py @baseArgs @collectArgs

Write-Host "Build finished. EXE output in $distPath\$name\$name.exe" -ForegroundColor Green
