from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ConversionOptions, TaskPlan


def write_manifest(
    task: TaskPlan,
    options: ConversionOptions,
    status: str,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "task_id": task.task_id,
        "source": str(task.source.source_path),
        "output_dir": str(task.output_dir),
        "target": options.target.value,
        "lerobot_version": options.lerobot_version,
        "conversion_mode": _conversion_mode(options),
        "lerobot_target_version": options.lerobot_version if options.target.value == "lerobot" else None,
        "fps": options.fps,
        "bag_type": options.bag_type,
        "status": status,
        "error": error,
        "source_is_zip": task.source.is_zip,
        "input_kind": task.input_kind,
        "adapter_used": task.adapter_used,
        "adapter_workdir": task.adapter_workdir,
        "runtime_mode": task.runtime_mode,
        "runtime_diagnostic": task.runtime_diagnostic,
        "path_strategy": task.path_strategy,
        "path_risk_level": task.path_risk_level,
        "path_risk_reason": task.path_risk_reason,
        "stage_workdir": task.stage_workdir,
        "error_details": task.error_details,
        "written_at": datetime.now().astimezone().isoformat(),
        "task": _json_safe(asdict(task)),
    }
    task.output_dir.mkdir(parents=True, exist_ok=True)
    (task.output_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _conversion_mode(options: ConversionOptions) -> str:
    if options.target.value != "lerobot":
        return "rosbag_real"
    if options.lerobot_version == "HDF5":
        return "hdf5_raw"
    return "lerobot_real"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value
