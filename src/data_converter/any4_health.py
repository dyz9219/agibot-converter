from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .any4_runtime import find_any4_python_for_version, is_force_bundled_enabled
from .any4lerobot_locator import find_any4lerobot_root

_TTL_SECONDS = 300.0
_CACHE: dict[tuple[str, str, str], tuple[float, "RuntimeCheckResult"]] = {}


@dataclass(slots=True)
class RuntimeCheckResult:
    ok: bool
    mode: str
    root: str
    python: str
    missing: list[str]
    diagnostic: str


def check_any4_runtime(version: str) -> RuntimeCheckResult:
    root = find_any4lerobot_root()
    py = find_any4_python_for_version(version)
    cache_key = (
        version,
        str(root.resolve()) if root is not None else "",
        str(py.resolve()) if py is not None else "",
    )
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached is not None and now - cached[0] < _TTL_SECONDS:
        return cached[1]

    bundled = _check_bundled_runtime(version, root)
    if bundled.ok:
        result = bundled
    else:
        external = _check_external_runtime(version, root, py)
        if external.ok:
            result = external
        else:
            missing = list(dict.fromkeys([*bundled.missing, *external.missing]))
            result = RuntimeCheckResult(
                ok=False,
                mode="none",
                root=external.root or bundled.root,
                python=external.python or bundled.python,
                missing=missing,
                diagnostic=f"{bundled.diagnostic}; fallback={external.diagnostic}",
            )
    _CACHE[cache_key] = (now, result)
    return result


def _check_bundled_runtime(version: str, root: Path | None) -> RuntimeCheckResult:
    missing: list[str] = []
    if root is None:
        missing.append("any4_root")
        return _result(False, "none", root, None, missing)

    inserted: list[str] = []
    try:
        lightweight = not getattr(sys, "frozen", False)
        psutil_probe = _probe_psutil_runtime(lightweight=lightweight)
        if psutil_probe != "ok":
            missing.append("psutil_runtime")
            return _result(
                False,
                "bundled",
                root,
                None,
                missing,
                detail=f"bundled_error={psutil_probe}",
            )
        ray_probe = _probe_ray_runtime(lightweight=lightweight)
        if ray_probe != "ok":
            missing.append("ray_runtime")
            return _result(
                False,
                "bundled",
                root,
                None,
                missing,
                detail=f"bundled_error={ray_probe}",
            )
        for p in [str(root), str(root / "agibot2lerobot")]:
            if p not in sys.path and Path(p).exists():
                sys.path.insert(0, p)
                inserted.append(p)
        if lightweight:
            if importlib.util.find_spec("agibot2lerobot.agibot_h5") is None:
                missing.append("agibot2lerobot_import")
                return _result(False, "bundled", root, None, missing)
        else:
            importlib.import_module("agibot2lerobot.agibot_h5")
        _check_version_scripts(version, root, missing)
        _check_version_runtime_deps(version, missing)
        if missing:
            return _result(False, "bundled", root, None, missing)
        return _result(True, "bundled", root, None, [])
    except Exception as exc:
        missing.append("agibot2lerobot_import")
        return _result(
            False,
            "bundled",
            root,
            None,
            missing,
            detail=f"bundled_error={type(exc).__name__}:{exc}",
        )
    finally:
        for p in inserted:
            try:
                sys.path.remove(p)
            except ValueError:
                pass


def _check_external_runtime(version: str, root: Path | None, py: Path | None) -> RuntimeCheckResult:
    missing: list[str] = []
    if py is None:
        missing.append("python")
        return _result(False, "none", root, py, missing, detail="external_error=python_not_found")

    code_parts = [
        "import importlib,sys",
        "from pathlib import Path",
    ]
    if root is not None:
        code_parts += [
            f"root=Path(r'{str(root)}')",
            "sys.path.insert(0,str(root))",
            "sys.path.insert(0,str(root/'agibot2lerobot'))",
        ]
    code_parts.append("importlib.import_module('agibot2lerobot.agibot_h5')")
    if version in {"v2.1", "v2.0"}:
        code_parts.append("import jsonlines")
    if root is not None and version in {"v2.1", "v2.0"}:
        code_parts.append(
            f"assert Path(r'{str(root / 'ds_version_convert' / 'v30_to_v21' / 'convert_dataset_v30_to_v21.py')}').exists()"
        )
    if root is not None and version == "v2.0":
        code_parts.append(
            f"assert Path(r'{str(root / 'ds_version_convert' / 'v21_to_v20' / 'convert_dataset_v21_to_v20.py')}').exists()"
        )
    code_parts.append("print('OK')")
    code = ";".join(code_parts)

    proc = _run_hidden_subprocess([str(py), "-c", code])
    if proc.returncode == 0 and "OK" in (proc.stdout or ""):
        return _result(True, "external", root, py, [])
    missing.append("external_probe_failed")
    stderr = (proc.stderr or "").strip().replace("\n", " ")
    stdout = (proc.stdout or "").strip().replace("\n", " ")
    detail = f"external_error=probe_failed:rc={proc.returncode}; stderr={stderr}; stdout={stdout}"
    return _result(False, "none", root, py, missing, detail=detail)


def _check_version_scripts(version: str, root: Path, missing: list[str]) -> None:
    if version in {"v2.1", "v2.0"} and not (
        root / "ds_version_convert" / "v30_to_v21" / "convert_dataset_v30_to_v21.py"
    ).exists():
        missing.append("v30_to_v21_script")
    if version == "v2.0" and not (
        root / "ds_version_convert" / "v21_to_v20" / "convert_dataset_v21_to_v20.py"
    ).exists():
        missing.append("v21_to_v20_script")


def _check_version_runtime_deps(version: str, missing: list[str]) -> None:
    if version in {"v2.1", "v2.0"} and importlib.util.find_spec("jsonlines") is None:
        missing.append("jsonlines")


def _result(
    ok: bool,
    mode: str,
    root: Path | None,
    py: Path | None,
    missing: list[str],
    detail: str = "",
) -> RuntimeCheckResult:
    root_text = str(root) if root is not None else ""
    py_text = str(py) if py is not None else ""
    force_bundled = is_force_bundled_enabled()
    force_text = "1" if force_bundled else "0"
    if ok:
        diagnostic = f"mode={mode}; root={root_text}; python={py_text}; force_bundled={force_text}"
    else:
        diagnostic = (
            f"mode={mode}; root={root_text}; python={py_text}; "
            f"force_bundled={force_text}; missing={','.join(missing)}"
        )
        if detail:
            diagnostic = f"{diagnostic}; {detail}"
    return RuntimeCheckResult(
        ok=ok,
        mode=mode,
        root=root_text,
        python=py_text,
        missing=missing,
        diagnostic=diagnostic,
    )


def _run_hidden_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def _probe_psutil_runtime(*, lightweight: bool) -> str:
    private_suffix = _platform_psutil_suffix()
    private_module = f"psutil.{private_suffix}"
    ray_private_module = f"ray.thirdparty_files.psutil.{private_suffix}"
    try:
        if lightweight:
            if importlib.util.find_spec("psutil") is None:
                raise ModuleNotFoundError("psutil")
            if importlib.util.find_spec(private_module) is None:
                raise ModuleNotFoundError(private_module)
            ray_dir = _package_dir_from_spec("ray")
            if ray_dir is not None:
                bundled_psutil_dir = ray_dir / "thirdparty_files" / "psutil"
                if bundled_psutil_dir.is_dir() and not any(bundled_psutil_dir.glob(f"{private_suffix}.*")):
                    raise ModuleNotFoundError(ray_private_module)
        else:
            importlib.import_module("psutil")
            importlib.import_module(private_module)
            ray_psutil_spec = _safe_find_spec("ray.thirdparty_files.psutil")
            if ray_psutil_spec is not None:
                importlib.import_module("ray.thirdparty_files.psutil")
                if _safe_find_spec(ray_private_module) is not None:
                    importlib.import_module(ray_private_module)
        return "ok"
    except Exception as exc:
        meipass = getattr(sys, "_MEIPASS", "")
        probe_parts = [f"{type(exc).__name__}:{exc}", f"meipass={meipass or '<none>'}"]
        probe_parts.append(f"private_module={private_module}")
        probe_parts.append(f"ray_private_module={ray_private_module}")
        if meipass:
            pdir = Path(meipass) / "psutil"
            if pdir.exists():
                names = sorted([p.name for p in pdir.iterdir() if p.is_file()])
                probe_parts.append(f"psutil_files={','.join(names[:12])}")
            else:
                probe_parts.append("psutil_dir_missing")
            ray_pdir = Path(meipass) / "ray" / "thirdparty_files" / "psutil"
            if ray_pdir.exists():
                ray_names = sorted([p.name for p in ray_pdir.iterdir() if p.is_file()])
                probe_parts.append(f"ray_psutil_files={','.join(ray_names[:12])}")
            else:
                probe_parts.append("ray_psutil_dir_missing")
        probe_parts.append(f"path0={sys.path[0] if sys.path else ''}")
        probe_parts.append(f"cwd={os.getcwd()}")
        return "; ".join(probe_parts)


def _probe_ray_runtime(*, lightweight: bool) -> str:
    required_attrs = ("init", "available_resources", "remote", "get", "shutdown")
    try:
        if importlib.util.find_spec("ray") is None:
            raise ModuleNotFoundError("ray")
        if importlib.util.find_spec("ray.runtime_env") is None:
            raise ModuleNotFoundError("ray.runtime_env")
        if lightweight:
            return "ok"

        ray = importlib.import_module("ray")
        runtime_env = importlib.import_module("ray.runtime_env")
        missing_attrs = [name for name in required_attrs if not hasattr(ray, name)]
        if missing_attrs:
            raise AttributeError(f"ray missing attrs: {','.join(missing_attrs)}")
        if not hasattr(runtime_env, "RuntimeEnv"):
            raise AttributeError("ray.runtime_env missing RuntimeEnv")
        return "ok"
    except Exception as exc:
        meipass = getattr(sys, "_MEIPASS", "")
        return f"{type(exc).__name__}:{exc}; meipass={meipass or '<none>'}"


def _platform_psutil_suffix() -> str:
    if sys.platform == "win32":
        return "_psutil_windows"
    if sys.platform.startswith("linux"):
        return "_psutil_linux"
    if sys.platform == "darwin":
        return "_psutil_osx"
    return "_psutil_posix"



def _safe_find_spec(name: str):
    try:
        return importlib.util.find_spec(name)
    except ModuleNotFoundError:
        return None

def _package_dir_from_spec(name: str) -> Path | None:
    spec = importlib.util.find_spec(name)
    if spec is None:
        return None
    locations = getattr(spec, "submodule_search_locations", None)
    if locations:
        first = next(iter(locations), None)
        if first:
            return Path(first)
    origin = getattr(spec, "origin", None)
    if origin:
        return Path(origin).parent
    return None
