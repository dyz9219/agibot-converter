from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def _read_repo_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_curated_environment_does_not_install_unused_large_dependencies() -> None:
    script = _read_repo_text("scripts/install_curated_env.ps1")
    removed_dependencies = [
        "diffusers",
        "cmake",
        "einops",
        "pynput",
        "pyserial",
        "wandb",
    ]

    for dependency in removed_dependencies:
        assert re.search(rf'"{re.escape(dependency)}[<>=\[]', script) is None


def test_packaging_scripts_do_not_collect_all_ray() -> None:
    packaging_scripts = [
        "scripts/build_exe.ps1",
        "scripts/build_exe_onefile.ps1",
        "scripts/smoke_lerobot_exe.ps1",
    ]

    for path in packaging_scripts:
        script = _read_repo_text(path)
        assert re.search(r"--collect-all\s+ray(?:\s|`|$)", script) is None
        assert '"--collect-all", "ray"' not in script
