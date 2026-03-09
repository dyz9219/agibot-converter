from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class PathRisk:
    is_windows: bool
    long_paths_enabled: bool | None
    max_candidate_len: int
    risk_level: str
    reason: str


def assess_path_risk(source_path: Path, output_dir: Path) -> PathRisk:
    is_windows = os.name == "nt"
    long_paths_enabled = _read_long_paths_enabled() if is_windows else None
    max_candidate_len = _estimate_max_candidate_len(source_path, output_dir)

    if not is_windows:
        return PathRisk(
            is_windows=False,
            long_paths_enabled=None,
            max_candidate_len=max_candidate_len,
            risk_level="low",
            reason=f"non-windows; max_candidate_len={max_candidate_len}",
        )

    if max_candidate_len >= 220:
        if long_paths_enabled is False:
            reason = f"max_candidate_len={max_candidate_len}, LongPathsEnabled=0"
        elif long_paths_enabled is True:
            reason = f"max_candidate_len={max_candidate_len}, LongPathsEnabled=1"
        else:
            reason = f"max_candidate_len={max_candidate_len}, LongPathsEnabled=unknown"
        return PathRisk(
            is_windows=True,
            long_paths_enabled=long_paths_enabled,
            max_candidate_len=max_candidate_len,
            risk_level="high",
            reason=reason,
        )

    if long_paths_enabled is False and max_candidate_len >= 180:
        return PathRisk(
            is_windows=True,
            long_paths_enabled=False,
            max_candidate_len=max_candidate_len,
            risk_level="high",
            reason=f"max_candidate_len={max_candidate_len}, LongPathsEnabled=0",
        )

    if max_candidate_len >= 170:
        return PathRisk(
            is_windows=True,
            long_paths_enabled=long_paths_enabled,
            max_candidate_len=max_candidate_len,
            risk_level="medium",
            reason=f"max_candidate_len={max_candidate_len}",
        )

    return PathRisk(
        is_windows=True,
        long_paths_enabled=long_paths_enabled,
        max_candidate_len=max_candidate_len,
        risk_level="low",
        reason=f"max_candidate_len={max_candidate_len}",
    )


def _estimate_max_candidate_len(source_path: Path, output_dir: Path) -> int:
    # Conservative estimates of deep paths produced by any4 / lerobot exports.
    candidates = [
        source_path,
        output_dir,
        output_dir / "meta" / "info.json",
        output_dir / "meta" / "episodes_stats.jsonl",
        output_dir / "data" / "chunk-000" / "file-000.parquet",
        output_dir / "videos" / "observation.images.back_right_fisheye" / "chunk-000" / "file-000.mp4",
    ]
    return max(len(str(p)) for p in candidates)


def _read_long_paths_enabled() -> bool | None:
    try:
        import winreg  # type: ignore
    except Exception:
        return None
    try:
        key_path = r"SYSTEM\CurrentControlSet\Control\FileSystem"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
            val, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
        return bool(int(val))
    except Exception:
        return None
