$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    python -m venv $venv
}

& (Join-Path $PSScriptRoot "install_curated_env.ps1")
& $py -m data_converter.main
