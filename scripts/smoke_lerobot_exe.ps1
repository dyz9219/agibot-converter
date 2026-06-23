param(
    [string]$InputPath = "",
    [string]$OutputRoot = "smoke-runs",
    [int]$Concurrency = 2,
    [string[]]$Versions = @("HDF5", "v2.0", "v2.1", "v3.0"),
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $InputPath) {
    $InputPath = Join-Path $root "演示用抓取任务_2013529099792277505_20260210_131921"
}

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
$any4Local = Join-Path $root "any4lerobot"
$any4Parent = Join-Path (Split-Path $root -Parent) "any4lerobot"
$any4 = if (Test-Path (Join-Path $any4Local "agibot2lerobot\agibot_h5.py")) { $any4Local } else { $any4Parent }
$distPath = Join-Path $root "dist-smoke"
$workPath = Join-Path $root "build-smoke"
$exe = Join-Path $distPath "DataConverterShell\DataConverterShell.exe"

function Remove-PathIfExists([string]$path) {
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path -ErrorAction Stop
    }
}

if (-not $SkipBuild) {
    if (-not (Test-Path $py)) {
        python -m venv $venv
    }

    & (Join-Path $PSScriptRoot "install_curated_env.ps1") -IncludeDev

    if (-not (Test-Path (Join-Path $any4 "agibot2lerobot\agibot_h5.py"))) {
        throw "Missing bundled any4lerobot source: $any4"
    }

    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -in @("DataConverterShell", "flet") } |
        Stop-Process -Force -ErrorAction SilentlyContinue

    Remove-PathIfExists $workPath
    Remove-PathIfExists $distPath

    & $py -m PyInstaller `
      --noconfirm `
      --windowed `
      --name DataConverterShell `
      --distpath $distPath `
      --workpath $workPath `
      --collect-all flet `
      --collect-all flet_desktop `
      --collect-submodules ray `
      --collect-binaries ray `
      --collect-all torch `
      --collect-all lerobot `
      --collect-all agibot_utils `
      --collect-all psutil `
      --hidden-import psutil._psutil_windows `
      --collect-all rosbags `
      --collect-submodules rosbags.typesys.stores `
      --add-data "$any4;any4lerobot" `
      src/data_converter/main.py
}

if (-not (Test-Path $exe)) {
    throw "EXE not found: $exe"
}

if (-not (Test-Path $InputPath)) {
    throw "Input path not found: $InputPath"
}

foreach ($healthVersion in @("v3.0", "v2.1", "v2.0")) {
    $healthProc = Start-Process -FilePath $exe -ArgumentList @("--internal-run-any4-health", "--version", $healthVersion) -Wait -PassThru -WindowStyle Hidden
    if ($healthProc.ExitCode -ne 0) {
        throw "ANY4 health check failed for version: $healthVersion"
    }
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runRoot = Join-Path $OutputRoot "exe-smoke-$stamp"
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$results = @()
$overallOk = $true

foreach ($version in $Versions) {
    $out = Join-Path $runRoot $version
    New-Item -ItemType Directory -Force -Path $out | Out-Null

    $argList = @(
        "--internal-run-conversion",
        "--input-path", (Resolve-Path $InputPath).Path,
        "--output-path", (Resolve-Path $out).Path,
        "--target", "lerobot",
        "--version", $version,
        "--concurrency", "$Concurrency"
    )

    $proc = Start-Process -FilePath $exe -ArgumentList $argList -Wait -PassThru -WindowStyle Hidden
    $exitCode = $proc.ExitCode

    $manifestSuccess = 0
    $manifestFailed = 0
    $versionMatches = 0
    $versionMismatches = 0
    $manifests = Get-ChildItem -Recurse -Filter "manifest.json" $out -ErrorAction SilentlyContinue
    foreach ($m in $manifests) {
        $data = Get-Content $m.FullName -Raw | ConvertFrom-Json
        if ($data.status -eq "success") {
            $manifestSuccess += 1
        } elseif ($data.status -eq "failed") {
            $manifestFailed += 1
        }
    }

    if ($version -ne "HDF5") {
        $infos = Get-ChildItem -Recurse -Filter "info.json" $out -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*\meta\info.json" }
        foreach ($i in $infos) {
            $info = Get-Content $i.FullName -Raw | ConvertFrom-Json
            if ($info.codebase_version -eq $version) {
                $versionMatches += 1
            } else {
                $versionMismatches += 1
            }
        }
        if ($versionMatches -eq 0 -or $versionMismatches -gt 0) {
            $overallOk = $false
        }
    }

    $fileCount = (Get-ChildItem -Recurse -Force $out | Measure-Object).Count
    if ($exitCode -ne 0 -or $manifestFailed -gt 0) {
        $overallOk = $false
    }

    $results += [PSCustomObject]@{
        version = $version
        exit_code = $exitCode
        output = $out
        file_count = $fileCount
        manifest_success = $manifestSuccess
        manifest_failed = $manifestFailed
        version_match = $versionMatches
        version_mismatch = $versionMismatches
    }
}

$summaryPath = Join-Path $runRoot "smoke-summary.json"
$results | ConvertTo-Json -Depth 4 | Set-Content -Path $summaryPath -Encoding UTF8

$results | Format-Table -AutoSize
Write-Host "Summary JSON: $summaryPath"

if (-not $overallOk) {
    exit 1
}

exit 0
