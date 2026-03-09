# Data Converter Backend Execution Plan

## Goal
Implement phase-2 backend for Agibot dataset conversion, wired to current Flet UI.

## Scope
- Support two conversion targets:
  - Agibot -> LeRobot (`v3.0`, `v2.1`, `v2.0`, `HDF5 raw`)
  - Agibot -> Rosbag (`MCAP`, `ROS2 .db3`, `ROS1 .bag`)
- Enforce precheck-before-start flow.
- Support mixed input scenarios (zip and extracted folders, both processed).
- Use single-process thread pool execution (`max_workers` from UI).
- Retry failed tasks once.
- Write per-task `manifest.json`.

## Constraints and Decisions
- Python style: PEP8 + ruff + black + mypy.
- Conflict policy: `skip`.
- Default concurrency: `4` (from UI config).
- LeRobot FPS: prefer source metadata, fallback to UI FPS.
- Rosbag path: use `uv` runtime.
- LeRobot path: use `any4lerobot-conversion` toolchain.
- For `LeRobot + HDF5`, output only `__hdf5_raw`.

## Implementation Steps
1. Add backend domain models (`models.py`).
2. Add source discovery (`discovery.py`) for all input scenarios.
3. Add precheck module (`precheck.py`) with blocking/non-blocking issues.
4. Add conversion runners:
   - `converters/lerobot_runner.py`
   - `converters/rosbag_runner.py`
5. Add manifest writer (`manifest.py`).
6. Add backend orchestrator (`backend.py`) with thread pool and retry.
7. Integrate UI actions:
   - `预检` button -> real precheck call
   - `开始转换` button -> conversion run
8. Validate with:
   - `python -m py_compile`
   - Manual precheck and start flow in UI

## Risks
- External tools may not be installed in local runtime (`uv`, `any4lerobot`).
- Real conversion command flags can vary by installed versions.

## Mitigations
- Precheck includes executable availability checks.
- Runtime errors are surfaced with clear actionable messages.

