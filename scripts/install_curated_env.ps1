param(
    [switch]$IncludeDev
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "Missing python venv: $py"
}

$baseDeps = @(
    "flet>=0.27,<1.0",
    "flet-desktop>=0.27,<1.0",
    "h5py>=3.10",
    "numpy>=1.26,<2",
    "opencv-python-headless>=4.9",
    "rosbags>=0.11",
    "ray[default]>=2.9",
    "torch==2.2.2",
    "torchvision==0.17.2",
    "datasets==4.1.1",
    "huggingface-hub==0.35.1",
    "pyarrow==21.0.0",
    "jsonlines>=4.0",
    "pillow>=10.0",
    "diffusers>=0.27.2,<0.36.0",
    "accelerate>=1.10.0,<2.0.0",
    "cmake>=3.29.0.1,<4.2.0",
    "einops>=0.8.0,<0.9.0",
    "av>=15.0.0,<16.0.0",
    "pynput>=1.7.7,<1.9.0",
    "pyserial>=3.5,<4.0",
    "wandb>=0.24.0,<0.25.0"
)

$devDeps = @(
    "black>=24.0",
    "isort>=5.13",
    "mypy>=1.10",
    "ruff>=0.8",
    "pytest>=8.0",
    "pyinstaller>=6.0"
)

& $py -m pip install --upgrade pip wheel
& $py -m pip install "setuptools<81.0.0,>=71.0.0"
& $py -m pip install @baseDeps
if ($IncludeDev) {
    & $py -m pip install @devDeps
}
& $py -m pip install --no-deps "lerobot==0.4.4"
if ($IncludeDev) {
    & $py -m pip install --no-deps -e ".[dev]"
} else {
    & $py -m pip install --no-deps -e .
}
