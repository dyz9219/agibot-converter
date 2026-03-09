$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    python -m venv $venv
}

& $py -m pip install -U pip
& $py -m pip install -e .
& $py -m data_converter.main
