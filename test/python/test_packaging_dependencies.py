from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def _read_repo_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_curated_environment_does_not_install_unused_large_dependencies() -> None:
    checked_files = [
        "scripts/install_curated_env.ps1",
        ".github/workflows/build.yml",
    ]
    removed_dependencies = [
        "diffusers",
        "cmake",
        "einops",
        "pynput",
        "pyserial",
        "wandb",
    ]

    for path in checked_files:
        script = _read_repo_text(path)
        for dependency in removed_dependencies:
            assert re.search(rf'"{re.escape(dependency)}[<>=\[]', script) is None


def test_packaging_scripts_do_not_collect_all_ray() -> None:
    packaging_scripts = [
        "scripts/build_exe.ps1",
        "scripts/build_exe_onefile.ps1",
        "scripts/smoke_lerobot_exe.ps1",
        ".github/workflows/build.yml",
    ]

    for path in packaging_scripts:
        script = _read_repo_text(path)
        assert re.search(r"--collect-all\s+ray(?:\s|`|$)", script) is None
        assert '"--collect-all", "ray"' not in script


def test_packaging_scripts_do_not_collect_all_torch() -> None:
    packaging_scripts = [
        "scripts/build_exe.ps1",
        "scripts/build_exe_onefile.ps1",
        "scripts/smoke_lerobot_exe.ps1",
        ".github/workflows/build.yml",
    ]

    for path in packaging_scripts:
        script = _read_repo_text(path)
        assert re.search(r"--collect-all\s+torch(?:\s|`|$)", script) is None
        assert re.search(r"--collect-submodules\s+torch(?:\s|`|$)", script) is None
        assert '"--collect-all", "torch"' not in script
        assert '"--collect-submodules", "torch"' not in script


def test_windows_full_package_uses_unqualified_distribution_name() -> None:
    workflow = _read_repo_text(".github/workflows/build.yml")
    build_script = _read_repo_text("scripts/build_exe.ps1")
    fingerprint_script = _read_repo_text("scripts/verify_build_fingerprint.ps1")

    assert "name: DataConverterShell-Windows\n" in workflow
    assert "DataConverterShell-Windows-full" not in workflow
    assert "dist/DataConverterShell/DataConverterShell.exe" in workflow
    assert "dist/DataConverterShell-full/DataConverterShell-full.exe" not in workflow
    assert '$name = "DataConverterShell"' in build_script
    assert '$name = "DataConverterShell-full"' not in build_script
    assert "dist\\DataConverterShell\\DataConverterShell.exe" in fingerprint_script
    assert "dist\\DataConverterShell-full\\DataConverterShell-full.exe" not in fingerprint_script


def test_build_fingerprint_ignores_generated_spec_file() -> None:
    for path in ["scripts/build_exe.ps1", "scripts/verify_build_fingerprint.ps1"]:
        script = _read_repo_text(path)
        assert 'root / "DataConverterShell.spec"' not in script


def test_build_fingerprint_normalizes_line_endings() -> None:
    for path in ["scripts/build_exe.ps1", "scripts/verify_build_fingerprint.ps1"]:
        script = _read_repo_text(path)
        assert 'replace(b"\\r\\n", b"\\n")' in script


def test_linux_x64_package_is_single_file_artifact() -> None:
    workflow = _read_repo_text(".github/workflows/build.yml")
    linux_x64 = workflow.split("  build-linux-x64:", 1)[1].split("  build-linux-arm64:", 1)[0]

    assert "--onefile" in linux_x64
    assert "--onedir" not in linux_x64
    assert "./dist/DataConverterShell --internal-run-any4-health --version v3.0" in linux_x64
    assert "path: dist/DataConverterShell\n" in linux_x64
    assert "path: dist/DataConverterShell/\n" not in linux_x64
