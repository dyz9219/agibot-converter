from __future__ import annotations

import os
import sys
from pathlib import Path


def is_force_bundled_enabled() -> bool:
    raw = os.environ.get("DATA_CONVERTER_FORCE_BUNDLED", "").strip().lower()
    if not raw:
        raw = os.environ.get("AGIBOT_FORCE_BUNDLED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # Default behavior for packaged EXE: force bundled runtime to avoid
    # accidental fallback to local Python environments on developer machines.
    return bool(getattr(sys, "frozen", False))


def _resolve_env_python(name: str) -> Path | None:
    env_py = os.environ.get(name, "").strip()
    if env_py:
        p = Path(env_py).expanduser()
        if p.exists():
            return p
    return None


def _get_venv_python(base_path: Path) -> Path:
    """Get the Python executable path for a virtual environment, handling platform differences."""
    if sys.platform == "win32":
        return base_path / ".venv" / "Scripts" / "python.exe"
    else:
        return base_path / ".venv" / "bin" / "python"


def find_any4_python() -> Path | None:
    p = _resolve_env_python("ANY4LEROBOT_PYTHON")
    if p is not None:
        return p

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        current = exe_dir
        for _ in range(6):
            candidates.append(_get_venv_python(current))
            current = current.parent
    else:
        repo_root = Path(__file__).resolve().parents[1]
        candidates.append(_get_venv_python(repo_root))

    cwd = Path.cwd().resolve()
    candidates.append(_get_venv_python(cwd))
    candidates.append(_get_venv_python(cwd.parent))

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def find_any4_python_for_version(version: str) -> Path | None:
    # Debug switch: force bundled any4 runtime path in packaged app,
    # so local machine can reproduce "no external python runtime" behavior.
    if version in {"v3.0", "v2.1", "v2.0"} and is_force_bundled_enabled():
        return None

    if version == "v2.0":
        p = _resolve_env_python("ANY4_RUNTIME_V21_PYTHON")
        if p is not None:
            return p
    elif version in {"v2.1", "v3.0"}:
        p = _resolve_env_python("ANY4_RUNTIME_V30_PYTHON")
        if p is not None:
            return p
    return find_any4_python()
