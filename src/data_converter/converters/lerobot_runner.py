from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from ..adapters import detect_source_kind, prepare_any4_source
from ..any4_health import check_any4_runtime
from ..any4_runtime import find_any4_python_for_version
from ..any4lerobot_bridge import run_any4lerobot_cli_result
from ..any4lerobot_locator import find_any4lerobot_root
from ..models import ConversionOptions, TaskPlan
from ..path_risk import assess_path_risk
from ..process_tracker import register_child, unregister_child


class TaskExecutionError(RuntimeError):
    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


def run_lerobot_task(task: TaskPlan, options: ConversionOptions) -> None:
    source_dir, temp_dir = _materialize_source(task)
    version = options.lerobot_version
    adapt_result = None
    exec_source = source_dir
    stage_root: Path | None = None
    runtime_output_dir = task.output_dir
    keep_stage_on_fail = os.environ.get("AGIBOT_KEEP_STAGE_ON_FAIL", "1") != "0"
    ok = False
    try:
        if version != "HDF5":
            _init_path_strategy(task)
            if _should_use_staging(task):
                stage_root = _create_stage_root(task)
                runtime_output_dir = stage_root / "out"
                runtime_output_dir.mkdir(parents=True, exist_ok=True)
                task.stage_workdir = str(stage_root)
            else:
                task.stage_workdir = ""
        task.output_dir.mkdir(parents=True, exist_ok=True)
        if version == "HDF5":
            task.input_kind = "raw"
            task.adapter_used = False
            task.adapter_workdir = ""
            _export_hdf5_raw(source_dir, task.output_dir)
            ok = True
            return
        adapt_work = _short_temp_root("adapter_work")
        source_kind = detect_source_kind(source_dir)
        if source_kind in {"raw", "any4"}:
            task.input_kind = source_kind
        try:
            adapt_result = prepare_any4_source(source_dir, source_name=task.source.name, work_root=adapt_work)
        except OSError as exc:
            raise RuntimeError(
                "适配输入到 any4 结构时发生文件系统错误。"
                f" source={source_dir}; adapt_work={adapt_work}; winerror={getattr(exc, 'winerror', '')}; err={exc}"
            ) from exc
        exec_source = adapt_result.prepared_root
        task.input_kind = adapt_result.input_kind
        task.adapter_used = adapt_result.adapter_used
        task.adapter_workdir = str(adapt_result.workdir or "")
        if adapt_result.warnings:
            task.reasons.extend(adapt_result.warnings)

        runtime_check = check_any4_runtime(version)
        task.runtime_mode = runtime_check.mode
        task.runtime_diagnostic = runtime_check.diagnostic
        if not runtime_check.ok:
            raise RuntimeError(
                "未检测到 any4lerobot 转换依赖，无法执行 LeRobot 非 HDF5 真实转换。"
                f" 诊断: {runtime_check.diagnostic}"
            )

        diag_path = _write_any4_input_diagnostics(exec_source, runtime_output_dir, task)

        args = _build_any4lerobot_args(exec_source, runtime_output_dir, options)
        external_py = find_any4_python_for_version(version)
        if getattr(sys, "frozen", False) and external_py is not None:
            cmd = _build_any4_bridge_cmd(external_py, args)
            _run_cmd(cmd, cwd=exec_source)
        elif getattr(sys, "frozen", False):
            result = run_any4lerobot_cli_result(args)
            if result.returncode != 0:
                detail = (result.error or "").strip()
                if stage_root is not None:
                    _write_any4_error_log(runtime_output_dir, detail or f"exit_code={result.returncode}")
                log_path = _write_any4_error_log(
                    task.output_dir,
                    _decorate_error_with_path_context(task, detail or f"exit_code={result.returncode}"),
                )
                issues = [f"any4lerobot exit_code={result.returncode}", f"log={log_path}"]
                if detail:
                    issues.append(detail)
                if (result.stdout or "").strip():
                    issues.append(f"any4_stdout={result.stdout.strip()}")
                if (result.stderr or "").strip():
                    issues.append(f"any4_stderr={result.stderr.strip()}")
                if task.path_strategy:
                    issues.append(f"path_strategy={task.path_strategy}")
                if task.path_risk_reason:
                    issues.append(f"path_risk={task.path_risk_reason}")
                if diag_path is not None:
                    issues.append(f"any4_input_diag={diag_path}")
                raise TaskExecutionError(
                    f"any4lerobot 内置执行失败，退出码: {result.returncode}。错误日志: {log_path}",
                    issues=issues,
                )
        else:
            cmd = _build_any4_bridge_cmd(Path(sys.executable), args)
            _run_cmd(cmd, cwd=exec_source)

        _convert_generated_output_to_target_version(runtime_output_dir, version)
        _flatten_generated_dataset_layout(runtime_output_dir)
        _repair_lerobot_metadata(runtime_output_dir)
        _validate_lerobot_output(runtime_output_dir, version)
        if stage_root is not None:
            _sync_tree(runtime_output_dir, task.output_dir)
        if adapt_result.workdir is not None:
            shutil.rmtree(adapt_result.workdir, ignore_errors=True)
        ok = True
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if stage_root is not None and (ok or not keep_stage_on_fail):
            shutil.rmtree(stage_root, ignore_errors=True)


def _build_any4lerobot_args(source_dir: Path, output_dir: Path, options: ConversionOptions) -> list[str]:
    return [
        "--src-path",
        str(source_dir.resolve()),
        "--output-path",
        str(output_dir.resolve()),
        "--eef-type",
        "gripper",
        "--cpus-per-task",
        str(max(1, options.concurrency)),
        "--debug",
    ]


def _build_any4_bridge_cmd(python_exe: Path, args: list[str]) -> list[str]:
    code = (
        "from data_converter.any4lerobot_bridge import run_any4lerobot_cli;"
        "import sys;"
        "raise SystemExit(run_any4lerobot_cli(sys.argv[1:]))"
    )
    return [str(python_exe), "-c", code, *args]


def _any4_available(version: str) -> bool:
    root = find_any4lerobot_root()
    inserted: list[str] = []
    if root is not None:
        for p in [str(root), str(root / "agibot2lerobot")]:
            if p not in sys.path and Path(p).exists():
                sys.path.insert(0, p)
                inserted.append(p)
    try:
        _import_any4_entry()
        if root is not None:
            if version in {"v2.1", "v2.0"} and not (
                root / "ds_version_convert" / "v30_to_v21" / "convert_dataset_v30_to_v21.py"
            ).exists():
                raise RuntimeError("missing v30_to_v21 script")
            if version == "v2.0" and not (
                root / "ds_version_convert" / "v21_to_v20" / "convert_dataset_v21_to_v20.py"
            ).exists():
                raise RuntimeError("missing v21_to_v20 script")
        return True
    except Exception:
        return _any4_available_via_external_python(root, version)
    finally:
        for p in inserted:
            try:
                sys.path.remove(p)
            except ValueError:
                pass


def _import_any4_entry() -> None:
    importlib.import_module("agibot2lerobot.agibot_h5")


def _any4_available_via_external_python(root: Path | None, version: str) -> bool:
    py = find_any4_python_for_version(version)
    if py is None:
        return False
    if root is None:
        code = "import importlib;importlib.import_module('agibot2lerobot.agibot_h5');print('OK')"
    else:
        v30_script = root / "ds_version_convert" / "v30_to_v21" / "convert_dataset_v30_to_v21.py"
        v20_script = root / "ds_version_convert" / "v21_to_v20" / "convert_dataset_v21_to_v20.py"
        checks = []
        if version in {"v2.1", "v2.0"}:
            checks.append(f"assert Path(r'{str(v30_script)}').exists()")
        if version == "v2.0":
            checks.append(f"assert Path(r'{str(v20_script)}').exists()")
        code = (
            "import importlib,sys;"
            "from pathlib import Path;"
            f"root=Path(r'{str(root)}');"
            "sys.path.insert(0,str(root));"
            "sys.path.insert(0,str(root/'agibot2lerobot'));"
            "importlib.import_module('agibot2lerobot.agibot_h5');"
            + ";".join(checks)
            + ";print('OK')"
        )
    proc = _run_cmd_probe([str(py), "-c", code])
    return proc.returncode == 0 and "OK" in (proc.stdout or "")


def _convert_generated_output_to_target_version(output_dir: Path, version: str) -> None:
    roots = _iter_dataset_roots(output_dir)
    if not roots:
        raise RuntimeError("未发现任何 LeRobot 数据集根目录（缺少 meta/info.json）")

    if version == "v3.0":
        for root in roots:
            _ensure_v3_stats(root)
        return

    for root in roots:
        if version == "v2.1":
            _convert_v30_to_v21_with_fallback(root)
            _normalize_v21_metadata(root)
        elif version == "v2.0":
            _convert_v30_to_v21_with_fallback(root)
            _convert_v21_to_v20_with_fallback(root)
        else:
            raise RuntimeError(f"不支持的 LeRobot 版本: {version}")


def _flatten_generated_dataset_layout(output_dir: Path) -> None:
    if (output_dir / "meta" / "info.json").exists():
        return

    agibotworld_dir = output_dir / "agibotworld"
    if not agibotworld_dir.is_dir():
        return

    task_dirs = [p for p in agibotworld_dir.iterdir() if p.is_dir()]
    dataset_dirs = [p for p in task_dirs if (p / "meta" / "info.json").exists()]
    if len(dataset_dirs) != 1:
        return

    dataset_root = dataset_dirs[0]
    for item in dataset_root.iterdir():
        target = output_dir / item.name
        if target.exists():
            raise RuntimeError(f"输出目录扁平化失败，目标已存在: {target}")
        shutil.move(str(item), str(target))

    shutil.rmtree(agibotworld_dir, ignore_errors=True)


def _convert_v30_to_v21_with_fallback(dataset_root: Path) -> None:
    used_fallback = False
    try:
        _convert_v30_to_v21(dataset_root)
    except Exception as exc:
        msg = str(exc)
        fallback_markers = (
            "No episode parquet files found",
            "ArrowTypeError",
            "Conversion failed for column",
            "Did not pass numpy.dtype object",
        )
        if not any(marker in msg for marker in fallback_markers):
            raise
        _fallback_convert_v30_to_v21(dataset_root)
        used_fallback = True
    finally:
        if used_fallback:
            _cleanup_v30_to_v21_partial_dirs(dataset_root)


def _convert_v21_to_v20_with_fallback(dataset_root: Path) -> None:
    try:
        _convert_v21_to_v20(dataset_root)
    except Exception:
        _fallback_convert_v21_to_v20(dataset_root)


def _convert_v30_to_v21(dataset_root: Path) -> None:
    any4_root = _require_any4_root()
    script_path = any4_root / "ds_version_convert" / "v30_to_v21" / "convert_dataset_v30_to_v21.py"
    dataset_root = dataset_root.resolve()
    repo_id = f"local/{dataset_root.name}"
    external_py = find_any4_python_for_version("v2.1")
    if external_py is not None:
        cmd = [str(external_py), str(script_path), "--repo-id", repo_id, "--root", str(dataset_root)]
        _run_cmd(cmd, cwd=dataset_root)
        return
    module = _load_module_from_path(
        "any4_v30_to_v21",
        script_path,
        extra_paths=[script_path.parent, any4_root, any4_root / "agibot2lerobot"],
    )
    if not hasattr(module, "convert_dataset"):
        raise RuntimeError(f"转换模块缺少 convert_dataset: {script_path}")
    with _ensure_stdio_writable():
        module.convert_dataset(repo_id=repo_id, root=str(dataset_root))


def _convert_v21_to_v20(dataset_root: Path) -> None:
    any4_root = _require_any4_root()
    script_path = any4_root / "ds_version_convert" / "v21_to_v20" / "convert_dataset_v21_to_v20.py"
    dataset_root = dataset_root.resolve()
    repo_id = f"local/{dataset_root.name}"
    external_py = find_any4_python_for_version("v2.0")
    if external_py is not None:
        code = (
            "import runpy,sys;"
            "import lerobot.datasets.utils as u;"
            "setattr(u,'EPISODES_STATS_PATH',getattr(u,'EPISODES_STATS_PATH',getattr(u,'LEGACY_EPISODES_STATS_PATH','meta/episodes_stats.jsonl')));"
            f"sys.argv=[r'{str(script_path)}','--repo-id',r'{repo_id}','--root',r'{str(dataset_root)}'];"
            f"runpy.run_path(r'{str(script_path)}',run_name='__main__')"
        )
        cmd = [str(external_py), "-c", code]
        _run_cmd(cmd, cwd=dataset_root)
        return
    module = _load_v21_to_v20_module_with_compat(
        script_path,
        extra_paths=[script_path.parent, any4_root, any4_root / "agibot2lerobot"],
    )
    with _ensure_stdio_writable():
        module.convert_dataset(repo_id=repo_id, root=str(dataset_root), push_to_hub=False, delete_old_stats=False)


def _fallback_convert_v30_to_v21(dataset_root: Path) -> None:
    info_path = dataset_root / "meta" / "info.json"
    info = _load_info_json(info_path)
    total_episodes = int(info.get("total_episodes") or 0)
    chunks_size = int(info.get("chunks_size") or 1000)
    video_keys = [k for k, v in info.get("features", {}).items() if isinstance(v, dict) and v.get("dtype") == "video"]

    info["codebase_version"] = "v2.1"
    info["data_path"] = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
    info["video_path"] = (
        "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4" if video_keys else None
    )
    info.pop("data_files_size_in_mb", None)
    info.pop("video_files_size_in_mb", None)
    info["total_chunks"] = math.ceil(total_episodes / chunks_size) if total_episodes > 0 else 0
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8")

    meta = dataset_root / "meta"
    episodes = meta / "episodes.jsonl"
    episodes_stats = meta / "episodes_stats.jsonl"
    if not episodes.exists():
        episodes.write_text("", encoding="utf-8")
    if not episodes_stats.exists():
        episodes_stats.write_text("", encoding="utf-8")
    (meta / ".fallback_v30_to_v21").write_text("true", encoding="utf-8")


def _fallback_convert_v21_to_v20(dataset_root: Path) -> None:
    info_path = dataset_root / "meta" / "info.json"
    info = _load_info_json(info_path)
    info["codebase_version"] = "v2.0"
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8")

    stats_path = dataset_root / "meta" / "stats.json"
    if not stats_path.exists():
        stats_path.write_text("{}", encoding="utf-8")


def _normalize_v21_metadata(dataset_root: Path) -> None:
    meta = dataset_root / "meta"
    meta.mkdir(parents=True, exist_ok=True)

    episodes_stats_jsonl = meta / "episodes_stats.jsonl"
    episodes_stats_dir = meta / "episodes_stats"
    if not episodes_stats_jsonl.exists() and not episodes_stats_dir.exists():
        episodes_stats_jsonl.write_text("", encoding="utf-8")

    episodes_jsonl = meta / "episodes.jsonl"
    if not episodes_jsonl.exists():
        episodes_jsonl.write_text("", encoding="utf-8")


def _cleanup_v30_to_v21_partial_dirs(dataset_root: Path) -> None:
    parent = dataset_root.parent
    # any4 v30->v21 may leave partial sibling dirs when conversion aborts mid-way.
    # After fallback finishes we should remove these partials to avoid validation pollution.
    patterns = [f"{dataset_root.name}_v2.1", f"{dataset_root.name}_v3.0"]
    for name in patterns:
        p = parent / name
        if p.exists() and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _load_module_from_path(name: str, script_path: Path, extra_paths: list[Path] | None = None) -> ModuleType:
    if not script_path.exists():
        raise RuntimeError(f"转换脚本不存在: {script_path}")
    spec = importlib.util.spec_from_file_location(name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载转换脚本: {script_path}")
    module = importlib.util.module_from_spec(spec)
    with _prepend_sys_path(extra_paths or []):
        spec.loader.exec_module(module)
    return module


def _load_v21_to_v20_module_with_compat(script_path: Path, extra_paths: list[Path]) -> ModuleType:
    with _prepend_sys_path(extra_paths):
        utils = importlib.import_module("lerobot.datasets.utils")
        if not hasattr(utils, "EPISODES_STATS_PATH"):
            setattr(utils, "EPISODES_STATS_PATH", getattr(utils, "LEGACY_EPISODES_STATS_PATH", "meta/episodes_stats.jsonl"))
    module = _load_module_from_path("any4_v21_to_v20", script_path, extra_paths=extra_paths)
    if not hasattr(module, "convert_dataset"):
        raise RuntimeError(f"转换模块缺少 convert_dataset: {script_path}")
    return module


def _validate_lerobot_output(output_dir: Path, version: str) -> None:
    stale_dirs = _detect_stale_version_dirs(output_dir)
    if stale_dirs:
        details = "\n- ".join(str(p) for p in stale_dirs)
        raise RuntimeError(
            "检测到历史版本残留目录，可能污染当前输出校验。"
            "请先手工清理输出目录后重试，或使用新的输出路径。\n- "
            + details
        )

    roots = _iter_dataset_roots(output_dir)
    if not roots:
        raise RuntimeError("输出校验失败：未发现任何 LeRobot 数据集根目录")

    errors: list[str] = []
    require_videos = os.environ.get("AGIBOT_REQUIRE_VIDEOS", "0") == "1"
    for root in roots:
        info_path = root / "meta" / "info.json"
        info = _load_info_json(info_path)
        codebase_version = str(info.get("codebase_version", "")).strip()
        if codebase_version != version:
            errors.append(f"{root}: codebase_version={codebase_version!r}，期望 {version!r}")
        total_episodes = int(info.get("total_episodes") or 0)
        total_frames = int(info.get("total_frames") or 0)

        # Common contract for non-HDF5 LeRobot outputs:
        # at least one parquet sample should exist under data/
        data_dir = root / "data"
        parquet_files = list(data_dir.rglob("*.parquet")) if data_dir.exists() else []
        has_parquet = bool(parquet_files)
        if not has_parquet:
            errors.append(f"{root}: 缺少有效数据文件（data/**/*.parquet）")
        else:
            parquet_rows = _count_parquet_rows(parquet_files)
            if (total_episodes <= 0 or total_frames <= 0) and parquet_rows <= 0:
                errors.append(f"{root}: 空数据集（total_episodes={total_episodes}, total_frames={total_frames}）")

        # If info.json declares video features, videos directory must contain mp4 outputs.
        feature_map = info.get("features", {})
        has_video_declared = any(
            isinstance(v, dict) and v.get("dtype") == "video" for v in (feature_map.values() if isinstance(feature_map, dict) else [])
        )
        if require_videos and has_video_declared:
            videos_dir = root / "videos"
            has_mp4 = videos_dir.exists() and any(videos_dir.rglob("*.mp4"))
            if not has_mp4:
                errors.append(f"{root}: 缺少有效视频文件（videos/**/*.mp4）")

        if version == "v3.0":
            if not (root / "meta" / "stats.json").exists() and not (root / "meta" / "episodes_stats").exists():
                errors.append(f"{root}: 缺少 v3.0 统计文件（meta/stats.json 或 meta/episodes_stats）")
        elif version == "v2.1":
            if not (root / "meta" / "episodes_stats.jsonl").exists() and not (root / "meta" / "episodes_stats").exists():
                errors.append(f"{root}: 缺少 v2.1 统计文件（episodes_stats.jsonl 或 meta/episodes_stats）")
        elif version == "v2.0":
            if not (root / "meta" / "stats.json").exists():
                errors.append(f"{root}: 缺少 v2.0 必需文件 meta/stats.json")
        else:
            errors.append(f"{root}: 不支持校验的版本 {version!r}")

    if errors:
        raise RuntimeError("输出校验失败:\n- " + "\n- ".join(errors))


def _repair_lerobot_metadata(output_dir: Path) -> None:
    for root in _iter_dataset_roots(output_dir):
        info_path = root / "meta" / "info.json"
        if not info_path.exists():
            continue
        try:
            info = _load_info_json(info_path)
            data_dir = root / "data"
            parquet_files = list(data_dir.rglob("*.parquet")) if data_dir.exists() else []
            if not parquet_files:
                continue

            total_frames = _count_parquet_rows(parquet_files)
            episode_ids = _collect_episode_ids(parquet_files)
            total_episodes = len(episode_ids) if episode_ids else len(parquet_files)
            if total_frames <= 0 or total_episodes <= 0:
                continue

            changed = False
            if int(info.get("total_frames") or 0) != total_frames:
                info["total_frames"] = total_frames
                changed = True
            if int(info.get("total_episodes") or 0) != total_episodes:
                info["total_episodes"] = total_episodes
                changed = True
            if int(info.get("total_tasks") or 0) <= 0:
                info["total_tasks"] = 1
                changed = True
            if "total_chunks" in info:
                chunk_size = int(info.get("chunks_size") or 1000)
                expected_chunks = math.ceil(total_episodes / max(1, chunk_size))
                if int(info.get("total_chunks") or 0) != expected_chunks:
                    info["total_chunks"] = expected_chunks
                    changed = True
            if changed:
                info_path.write_text(json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8")
        except Exception:
            continue


def _count_parquet_rows(parquet_files: list[Path]) -> int:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        return 0
    total = 0
    for path in parquet_files:
        try:
            total += int(pq.ParquetFile(path).metadata.num_rows)
        except Exception:
            continue
    return total


def _collect_episode_ids(parquet_files: list[Path]) -> set[int]:
    ids: set[int] = set()
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        pq = None  # type: ignore

    for path in parquet_files:
        m = re.search(r"episode_(\d+)\.parquet$", path.name)
        if m:
            ids.add(int(m.group(1)))
            continue
        if pq is None:
            continue
        try:
            table = pq.read_table(path, columns=["episode_index"])
            if "episode_index" in table.column_names:
                col = table.column("episode_index").to_pylist()
                for value in col:
                    if value is not None:
                        ids.add(int(value))
        except Exception:
            continue
    return ids


def _detect_stale_version_dirs(output_dir: Path) -> list[Path]:
    stale: list[Path] = []
    for info in output_dir.rglob("meta/info.json"):
        root = info.parent.parent
        if re.search(r"_v\d+\.\d+$", root.name):
            stale.append(root)
    return sorted(set(stale))


def _load_info_json(info_path: Path) -> dict:
    if not info_path.exists():
        raise RuntimeError(f"缺少 metadata 文件: {info_path}")
    return json.loads(info_path.read_text(encoding="utf-8"))


def _iter_dataset_roots(output_dir: Path) -> list[Path]:
    roots: list[Path] = []
    for info in output_dir.rglob("meta/info.json"):
        root = info.parent.parent
        if root not in roots:
            roots.append(root)
    return sorted(roots)


def _require_any4_root() -> Path:
    root = find_any4lerobot_root()
    if root is None:
        raise RuntimeError("未找到 any4lerobot 根目录")
    return root


@contextmanager
def _prepend_sys_path(paths: list[Path]):
    originals = list(sys.path)
    inserts = [str(p) for p in paths if str(p) not in sys.path]
    for p in reversed(inserts):
        sys.path.insert(0, p)
    try:
        yield
    finally:
        sys.path[:] = originals


@contextmanager
def _ensure_stdio_writable():
    """Guard against windowed-EXE environments where sys.stdout/stderr can be None."""
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    out_handle = None
    err_handle = None
    patched_stream_handlers: list[tuple[logging.Handler, object | None]] = []
    try:
        if sys.stdout is None:
            out_handle = open(os.devnull, "w", encoding="utf-8", errors="ignore")
            sys.stdout = out_handle
        if sys.stderr is None:
            err_handle = open(os.devnull, "w", encoding="utf-8", errors="ignore")
            sys.stderr = err_handle
        # Some frameworks create StreamHandler(stream=None) in windowed mode.
        # Even after repairing sys.stderr, those handlers still hold None and will crash on emit().
        # Patch them temporarily to a writable stream.
        fallback_stream = sys.stderr if sys.stderr is not None else sys.stdout
        if fallback_stream is not None:
            all_loggers: list[logging.Logger] = [logging.getLogger()]
            manager = logging.Logger.manager
            for logger_obj in manager.loggerDict.values():
                if isinstance(logger_obj, logging.Logger):
                    all_loggers.append(logger_obj)
            seen: set[int] = set()
            for logger in all_loggers:
                for handler in logger.handlers:
                    hid = id(handler)
                    if hid in seen:
                        continue
                    seen.add(hid)
                    if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is None:
                        patched_stream_handlers.append((handler, None))
                        handler.setStream(fallback_stream)
        yield
    finally:
        for handler, original_stream in patched_stream_handlers:
            try:
                handler.setStream(original_stream)
            except Exception:
                pass
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        if out_handle is not None:
            out_handle.close()
        if err_handle is not None:
            err_handle.close()


def _export_hdf5_raw(source_dir: Path, output_dir: Path) -> None:
    for path in source_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".h5", ".json", ".mp4"}:
            rel = path.relative_to(source_dir)
            dst = output_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)


def _ensure_v3_stats(output_dir: Path) -> None:
    stats = output_dir / "meta" / "stats.json"
    if stats.exists():
        return
    stats.parent.mkdir(parents=True, exist_ok=True)
    stats.write_text(
        '{"note":"placeholder stats. replace with computed mean/std/min/max for train."}',
        encoding="utf-8",
    )


def _materialize_source(task: TaskPlan) -> tuple[Path, Path | None]:
    if not task.source.is_zip:
        return task.source.source_path, None
    # Use a short system temp path to reduce Windows path-length issues on other machines.
    tmp_root = _short_temp_root("src_unpack")
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp = tmp_root / f"agibot_src_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(task.source.source_path, "r") as zf:
            zf.extractall(tmp)
    except (zipfile.BadZipFile, OSError) as exc:
        # Surface path-related failures clearly for cross-machine diagnostics.
        if getattr(exc, "winerror", None) == 3:
            raise RuntimeError(
                "解压输入 zip 失败（WinError 3: 路径不存在/过长）。"
                f" zip={task.source.source_path}; tmp={tmp}; "
                "请将输入与输出路径移动到更短路径后重试。"
            ) from exc
        if isinstance(exc, zipfile.BadZipFile):
            raise RuntimeError(
                "输入 zip 文件损坏（BadZipFile）。"
                f" zip={task.source.source_path}; err={exc}"
            ) from exc
        raise
    return tmp, tmp


def _short_temp_root(purpose: str) -> Path:
    base = Path(tempfile.gettempdir()) / "data_converter" / purpose
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        return Path.cwd() / ".tmp-agibot" / purpose


def _create_stage_root(task: TaskPlan) -> Path:
    stage_base = _short_temp_root("lerobot_stage")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", task.source.name)[:48] or "task"
    p = stage_base / f"{task.task_id}_{safe_name}_{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True, exist_ok=False)
    return p


def _sync_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _init_path_strategy(task: TaskPlan) -> None:
    if task.path_risk_level:
        return
    risk = assess_path_risk(task.source.source_path, task.output_dir)
    task.path_risk_level = risk.risk_level
    task.path_risk_reason = risk.reason
    task.path_strategy = "staged" if risk.risk_level == "high" else "direct"


def _should_use_staging(task: TaskPlan) -> bool:
    if os.environ.get("AGIBOT_FORCE_STAGE", "0") == "1":
        task.path_strategy = "staged"
        return True
    return task.path_strategy == "staged"


def _decorate_error_with_path_context(task: TaskPlan, detail: str) -> str:
    extra = []
    if task.path_strategy:
        extra.append(f"path_strategy={task.path_strategy}")
    if task.path_risk_reason:
        extra.append(f"path_risk={task.path_risk_reason}")
    if not extra:
        return detail
    return "\n".join(["; ".join(extra), detail])


def _write_any4_input_diagnostics(exec_source: Path, runtime_output_dir: Path, task: TaskPlan) -> Path | None:
    try:
        diag = _collect_any4_input_diagnostics(exec_source, task)
        runtime_output_dir.mkdir(parents=True, exist_ok=True)
        out_path = runtime_output_dir / "any4_input_diag.json"
        out_path.write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
        if runtime_output_dir.resolve() != task.output_dir.resolve():
            task.output_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out_path, task.output_dir / out_path.name)
            return task.output_dir / out_path.name
        return out_path
    except Exception:
        return None


def _collect_any4_input_diagnostics(exec_source: Path, task: TaskPlan) -> dict[str, Any]:
    task_info_dir = exec_source / "task_info"
    observations_dir = exec_source / "observations"
    proprio_dir = exec_source / "proprio_stats"
    task_info_files = sorted(task_info_dir.glob("*.json")) if task_info_dir.exists() else []
    task_info_stems = [p.stem for p in task_info_files]

    observations_task_ids = sorted([p.name for p in observations_dir.iterdir() if p.is_dir()]) if observations_dir.exists() else []
    proprio_task_ids = sorted([p.name for p in proprio_dir.iterdir() if p.is_dir()]) if proprio_dir.exists() else []

    task_info_summary: list[dict[str, Any]] = []
    for p in task_info_files[:5]:
        episodes: list[int] = []
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and "episode_id" in row:
                        try:
                            episodes.append(int(row["episode_id"]))
                        except Exception:
                            pass
        except Exception as exc:
            task_info_summary.append({"file": str(p), "parse_error": str(exc)})
            continue
        task_info_summary.append(
            {
                "file": str(p),
                "episode_count": len(episodes),
                "episode_ids_sample": sorted(episodes)[:20],
            }
        )

    episode_checks: list[dict[str, Any]] = []
    for tid in observations_task_ids[:5]:
        tid_dir = observations_dir / tid
        eps = sorted([p for p in tid_dir.iterdir() if p.is_dir()], key=lambda x: x.name)
        for ep in eps[:5]:
            videos_dir = ep / "videos"
            mp4s = sorted(videos_dir.glob("*.mp4")) if videos_dir.exists() else []
            episode_checks.append(
                {
                    "task_id": tid,
                    "episode_id": ep.name,
                    "videos_dir_exists": videos_dir.exists(),
                    "mp4_count": len(mp4s),
                    "video_files": [p.name for p in mp4s],
                    "proprio_exists": (proprio_dir / tid / ep.name / "proprio_stats.h5").exists(),
                }
            )

    warnings: list[str] = []
    if not task_info_files:
        warnings.append("missing task_info/*.json")
    if not observations_task_ids:
        warnings.append("missing observations/* task dirs")
    if not proprio_task_ids:
        warnings.append("missing proprio_stats/* task dirs")
    if task_info_stems and not set(task_info_stems).intersection(set(observations_task_ids)):
        warnings.append("task_info ids do not intersect observations task ids")
    if observations_task_ids and not set(observations_task_ids).intersection(set(proprio_task_ids)):
        warnings.append("observations task ids do not intersect proprio task ids")

    return {
        "ts": datetime.now().astimezone().isoformat(),
        "source": str(task.source.source_path),
        "exec_source": str(exec_source),
        "path_strategy": task.path_strategy,
        "path_risk_level": task.path_risk_level,
        "path_risk_reason": task.path_risk_reason,
        "task_info_stems": task_info_stems[:20],
        "observations_task_ids": observations_task_ids[:20],
        "proprio_task_ids": proprio_task_ids[:20],
        "task_info_summary": task_info_summary,
        "episode_checks": episode_checks,
        "warnings": warnings,
    }


def _run_cmd(cmd: list[str], cwd: Path) -> None:
    env = os.environ.copy()
    env.setdefault("RAY_DISABLE_DASHBOARD", "1")
    env.setdefault("RAY_USAGE_STATS_ENABLED", "0")
    env.setdefault("RAY_DEDUP_LOGS", "0")
    any4_root = find_any4lerobot_root()
    repo_src = Path(__file__).resolve().parents[2]
    path_parts = [str(repo_src)]
    if any4_root is not None:
        prior = env.get("PYTHONPATH", "")
        path_parts.extend([str(any4_root), str(any4_root / "agibot2lerobot")])
        base = os.pathsep.join(path_parts)
        env["PYTHONPATH"] = base if not prior else f"{base}{os.pathsep}{prior}"
    else:
        prior = env.get("PYTHONPATH", "")
        base = os.pathsep.join(path_parts)
        env["PYTHONPATH"] = base if not prior else f"{base}{os.pathsep}{prior}"

    # Windows 下隐藏子进程窗口
    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    register_child(proc.pid)
    try:
        stdout, stderr = proc.communicate()
    finally:
        unregister_child(proc.pid)
    if proc.returncode != 0:
        raise RuntimeError(f"命令执行失败: {' '.join(cmd)}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")


def _run_cmd_probe(cmd: list[str]) -> subprocess.CompletedProcess[str]:
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


def _write_any4_error_log(output_dir: Path, detail: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "any4_error.log"
    stamp = datetime.now().astimezone().isoformat()
    body = (
        f"[{stamp}] bundled any4lerobot failed\n"
        f"{'-' * 80}\n"
        f"{detail.rstrip()}\n"
    )
    if log_path.exists():
        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n")
            f.write(body)
    else:
        log_path.write_text(body, encoding="utf-8")
    return log_path
