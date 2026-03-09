# Data Converter (UI Shell)

This is a phase-1 scaffold for a desktop converter tool.

Current scope:
- Flet desktop UI shell only
- macOS-inspired minimal silver visual style
- no real backend conversion logic yet
- can run locally and can be packaged into an `.exe`

## Quick Start

```powershell
cd D:\workspace\work\bwy\agibot-converter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
python -m data_converter.main
```

## Build EXE (Windows)

```powershell
cd D:\workspace\work\bwy\agibot-converter
.\scripts\build_exe.ps1
```

The build script bundles `..\any4lerobot` into the EXE output directory as `any4lerobot/`.
LeRobot conversion uses this bundled module at runtime, so end users do not need to install
`any4lerobot` manually.

Output:
- `dist/DataConverterShell/DataConverterShell.exe`

## EXE Smoke Regression (LeRobot)

```powershell
cd D:\workspace\work\bwy\agibot-converter
.\scripts\smoke_lerobot_exe.ps1
```

Optional: skip build and reuse existing `dist-smoke`:

```powershell
.\scripts\smoke_lerobot_exe.ps1 -SkipBuild
```

## Notes

- This shell is intentionally backend-free for visual review.
- Next phase can plug real conversion services into `src/data_converter/backend.py`.
