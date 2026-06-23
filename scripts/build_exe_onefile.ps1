param(
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
    if (Test-Path "$root\build-onefile") { Remove-Item -Recurse -Force "$root\build-onefile" }
    if (Test-Path "$root\dist-onefile") { Remove-Item -Recurse -Force "$root\dist-onefile" }
}

& (Join-Path $PSScriptRoot "install_curated_env.ps1") -IncludeDev

if (-not (Test-Path (Join-Path $any4 "agibot2lerobot\agibot_h5.py"))) {
    throw "Missing bundled any4lerobot source: $any4"
}
if (-not (Test-Path (Join-Path $assets "pku_logo.ico"))) {
    throw "Missing app icon asset: $assets\pku_logo.ico"
}

& $py -m PyInstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --name DataConverterShell `
  --distpath "$root\dist-onefile" `
  --workpath "$root\build-onefile" `
  --collect-all flet `
  --collect-all flet_desktop `
  --collect-submodules ray `
  --collect-binaries ray `
  --collect-all torch `
  --collect-all lerobot `
  --collect-all agibot_utils `
  --collect-all psutil `
  --hidden-import psutil._psutil_windows `
  --hidden-import ray.thirdparty_files.psutil._psutil_windows `
  --collect-all ray.thirdparty_files.psutil `
  --collect-all rosbags `
  --collect-submodules rosbags.typesys.stores `
  --collect-data tkinter `
  --icon "$assets\pku_logo.ico" `
  --add-data "$any4;any4lerobot" `
  --add-data "$assets;assets" `
  src/data_converter/main.py

Write-Host "Onefile build finished: dist-onefile/DataConverterShell.exe" -ForegroundColor Green
