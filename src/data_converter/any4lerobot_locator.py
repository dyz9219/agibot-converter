from __future__ import annotations

import os
import sys
from pathlib import Path


def find_any4lerobot_root() -> Path | None:
    seen: set[Path] = set()
    for candidate in _candidate_roots():
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_any4_root(resolved):
            return resolved
    return None


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []

    env_root = os.environ.get("ANY4LEROBOT_HOME", "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())

    exe_dir = Path(sys.executable).resolve().parent
    roots.append(exe_dir / "any4lerobot")

    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(Path(meipass) / "any4lerobot")

    here = Path(__file__).resolve()
    roots.append(here.parents[2] / "third_party" / "any4lerobot")
    roots.append(here.parents[3] / "any4lerobot")

    cwd = Path.cwd().resolve()
    roots.append(cwd / "any4lerobot")
    roots.append(cwd.parent / "any4lerobot")
    return roots


def _is_any4_root(path: Path) -> bool:
    return (path / "agibot2lerobot" / "agibot_h5.py").exists()
