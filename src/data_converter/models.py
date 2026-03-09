from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class TargetKind(str, Enum):
    LEROBOT = "lerobot"
    ROSBAG = "rosbag"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass(slots=True)
class ConversionOptions:
    input_path: Path
    output_path: Path
    target: TargetKind
    lerobot_version: str = "v3.0"
    fps: int = 30
    bag_type: str = "MCAP"
    concurrency: int = 4
    conflict_policy: str = "block"
    retry_limit: int = 1


@dataclass(slots=True)
class SourceItem:
    name: str
    source_path: Path
    is_zip: bool


@dataclass(slots=True)
class TaskPlan:
    task_id: str
    source: SourceItem
    output_dir: Path
    status: TaskStatus = TaskStatus.PENDING
    reasons: list[str] = field(default_factory=list)
    attempts: int = 0
    input_kind: str = ""
    adapter_used: bool = False
    adapter_workdir: str = ""
    runtime_mode: str = ""
    runtime_diagnostic: str = ""
    error_details: list[str] = field(default_factory=list)
    path_strategy: str = ""
    path_risk_level: str = ""
    path_risk_reason: str = ""
    stage_workdir: str = ""


@dataclass(slots=True)
class PrecheckResult:
    ok: bool
    ready: int
    skipped: int
    blocked: int
    global_errors: list[str]
    global_warnings: list[str]
    tasks: list[TaskPlan]
