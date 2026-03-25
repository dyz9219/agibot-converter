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
import threading
from io import BytesIO
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


_VERSION_POSTPROCESS_LOCK = threading.RLock()
_STDIO_GUARD_LOCK = threading.RLock()
_EMBED_VIDEO_KEYS = (
    "observation.images.head",
    "observation.images.hand_left",
    "observation.images.hand_right",
)
_RAW_IMAGE_CAMERA_DIRS = {
    "observation.images.head": "head_color",
    "observation.images.hand_left": "hand_left",
    "observation.images.hand_right": "hand_right",
}
_RAW_VIDEO_FILE_NAMES = {
    "observation.images.head": "head.mp4",
    "observation.images.hand_left": "hand_left.mp4",
    "observation.images.hand_right": "hand_right.mp4",
}
_V21_IMAGE_COLUMN_ORDER = (
    "observation.images.hand_left",
    "observation.images.hand_right",
    "observation.images.head",
)


class TaskExecutionError(RuntimeError):
    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


class _StageTimer:
    def __init__(self, task: TaskPlan, name: str) -> None:
        self._task = task
        self._name = name
        self._start = 0.0

    def __enter__(self):
        self._start = __import__("time").perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed = __import__("time").perf_counter() - self._start
        self._task.stage_timings[self._name] = round(elapsed, 6)


def run_lerobot_task(task: TaskPlan, options: ConversionOptions) -> None:
    task.stage_timings = {}
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
            with _StageTimer(task, "export_hdf5"):
                _export_hdf5_raw(source_dir, task.output_dir)
            ok = True
            return
        adapt_work = _short_temp_root("adapter_work")
        source_kind = detect_source_kind(source_dir)
        if source_kind in {"raw", "any4"}:
            task.input_kind = source_kind
        try:
            with _StageTimer(task, "prepare_any4_source"):
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

        with _StageTimer(task, "check_any4_runtime"):
            runtime_check = check_any4_runtime(version)
        task.runtime_mode = runtime_check.mode
        task.runtime_diagnostic = runtime_check.diagnostic
        if not runtime_check.ok:
            raise RuntimeError(
                "未检测到 any4lerobot 转换依赖，无法执行 LeRobot 非 HDF5 真实转换。"
                f" 诊断: {runtime_check.diagnostic}"
            )

        with _StageTimer(task, "write_input_diagnostics"):
            diag_path = _write_any4_input_diagnostics(exec_source, runtime_output_dir, task)

        args = _build_any4lerobot_args(
            exec_source,
            runtime_output_dir,
            options,
            debug=_should_use_any4_debug_mode(exec_source),
            inner_concurrency=max(1, task.lerobot_inner_concurrency),
        )
        external_py = find_any4_python_for_version(version)
        with _StageTimer(task, "run_any4lerobot"):
            if getattr(sys, "frozen", False) and external_py is not None:
                cmd = _build_any4_bridge_cmd(external_py, args)
                _run_cmd(cmd, cwd=exec_source)
            elif getattr(sys, "frozen", False):
                cmd = _build_frozen_any4_bridge_cmd(args)
                _run_cmd(cmd, cwd=exec_source)
            elif task.lerobot_inprocess_allowed:
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

        with _StageTimer(task, "postprocess_output"):
            _convert_generated_output_to_target_version(runtime_output_dir, version)
            _flatten_generated_dataset_layout(runtime_output_dir)
            _repair_lerobot_metadata(runtime_output_dir)
            raw_image_source = source_dir if task.input_kind == "raw" else None
            if version in {"v3.0", "v2.1", "v2.0"}:
                _embed_videos_in_parquet(runtime_output_dir, raw_source_dir=raw_image_source)
            elif options.embed_videos_in_parquet:
                _embed_videos_in_parquet(runtime_output_dir, raw_source_dir=raw_image_source)
            _validate_lerobot_output(runtime_output_dir, version)
        if stage_root is not None:
            with _StageTimer(task, "sync_tree"):
                _sync_tree(runtime_output_dir, task.output_dir)
        if adapt_result.workdir is not None:
            with _StageTimer(task, "cleanup_adapter_workdir"):
                shutil.rmtree(adapt_result.workdir, ignore_errors=True)
        ok = True
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if stage_root is not None and (ok or not keep_stage_on_fail):
            shutil.rmtree(stage_root, ignore_errors=True)


def _build_any4lerobot_args(
    source_dir: Path,
    output_dir: Path,
    options: ConversionOptions,
    *,
    debug: bool,
    inner_concurrency: int,
) -> list[str]:
    args = [
        "--src-path",
        str(source_dir.resolve()),
        "--output-path",
        str(output_dir.resolve()),
        "--eef-type",
        "gripper",
        "--cpus-per-task",
        str(max(1, inner_concurrency)),
    ]
    if debug:
        args.append("--debug")
    return args


def _should_use_any4_debug_mode(source_dir: Path) -> bool:
    task_info_dir = source_dir / "task_info"
    if not task_info_dir.is_dir():
        return True
    task_files = [p for p in task_info_dir.glob("*.json") if p.is_file()]
    return len(task_files) <= 1


def _build_any4_bridge_cmd(python_exe: Path, args: list[str]) -> list[str]:
    code = (
        "from data_converter.any4lerobot_bridge import run_any4lerobot_cli;"
        "import sys;"
        "raise SystemExit(run_any4lerobot_cli(sys.argv[1:]))"
    )
    return [str(python_exe), "-c", code, *args]


def _build_frozen_any4_bridge_cmd(args: list[str]) -> list[str]:
    return [str(Path(sys.executable)), "--internal-run-any4lerobot", *args]


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

    # v30->v21/v20 upgrade path mutates global module/logging state in bundled mode.
    # Serializing this postprocess avoids frozen-EXE races such as NoneType.write and partial syncs.
    with _VERSION_POSTPROCESS_LOCK:
        for root in roots:
            if version == "v2.1":
                _convert_v30_to_v21_with_fallback(root)
                _rewrite_v21_episode_parquet_schema(root)
                _normalize_v21_metadata(root)
                _cleanup_v30_artifacts_from_v21_layout(root)
            elif version == "v2.0":
                _convert_v30_to_v21_with_fallback(root)
                _convert_v21_to_v20_with_fallback(root)
            else:
                raise RuntimeError(f"不支持的 LeRobot 版本: {version}")


def _flatten_generated_dataset_layout(output_dir: Path) -> None:
    agibotworld_dir = output_dir / "agibotworld"
    if not agibotworld_dir.is_dir():
        return

    dataset_roots = _find_dataset_roots_under_agibotworld(agibotworld_dir)
    if len(dataset_roots) != 1:
        return

    dataset_root = dataset_roots[0]
    for item in dataset_root.iterdir():
        target = output_dir / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        shutil.move(str(item), str(target))

    shutil.rmtree(agibotworld_dir, ignore_errors=True)


def _find_dataset_roots_under_agibotworld(agibotworld_dir: Path) -> list[Path]:
    roots: list[Path] = []
    for info in agibotworld_dir.rglob("meta/info.json"):
        root = info.parent.parent
        if root not in roots:
            roots.append(root)
    return roots


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
    episode_records = _load_v30_episode_records(dataset_root)
    video_keys = [
        key
        for key, value in info.get("features", {}).items()
        if isinstance(value, dict) and value.get("dtype") == "video"
    ]

    _rewrite_info_for_v21(info_path, info, len(episode_records), video_keys)
    _write_legacy_tasks_jsonl(dataset_root)
    _split_v30_data_into_v21_files(dataset_root, episode_records)
    _write_legacy_episode_metadata(dataset_root, episode_records)
    _copy_or_split_v30_videos(dataset_root, episode_records, video_keys)
    _cleanup_v30_artifacts_from_v21_layout(dataset_root)
    (dataset_root / "meta" / ".fallback_v30_to_v21").write_text("true", encoding="utf-8")


def _fallback_convert_v21_to_v20(dataset_root: Path) -> None:
    info_path = dataset_root / "meta" / "info.json"
    info = _load_info_json(info_path)
    info["codebase_version"] = "v2.0"
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8")

    stats_path = dataset_root / "meta" / "stats.json"
    if not stats_path.exists():
        stats_path.write_text("{}", encoding="utf-8")


def _load_v30_episode_records(dataset_root: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"缺少 pyarrow，无法执行本地 v3.0->v2.1 fallback: {exc}") from exc

    episodes_dir = dataset_root / "meta" / "episodes"
    parquet_files = sorted(episodes_dir.rglob("*.parquet"))
    if not parquet_files:
        raise RuntimeError(f"缺少 v3.0 episode metadata parquet: {episodes_dir}")

    rows: list[dict[str, Any]] = []
    for path in parquet_files:
        rows.extend(pq.read_table(path).to_pylist())
    rows.sort(key=lambda item: int(item.get("episode_index", 0)))
    return rows


def _rewrite_info_for_v21(info_path: Path, info: dict[str, Any], total_episodes: int, video_keys: list[str]) -> None:
    chunks_size = int(info.get("chunks_size") or 1000)
    info["codebase_version"] = "v2.1"
    info["robot_type"] = "agibot"
    info["data_path"] = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
    info["video_path"] = (
        "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4" if video_keys else None
    )
    info.pop("data_files_size_in_mb", None)
    info.pop("video_files_size_in_mb", None)
    info["total_episodes"] = total_episodes
    info["total_chunks"] = math.ceil(total_episodes / chunks_size) if total_episodes > 0 else 0
    info["total_videos"] = 0
    for key, feature in info.get("features", {}).items():
        if isinstance(feature, dict) and feature.get("dtype") != "video":
            feature.pop("fps", None)
    info.pop("video_embedding", None)
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8")


def _write_legacy_tasks_jsonl(dataset_root: Path) -> None:
    tasks_parquet = dataset_root / "meta" / "tasks.parquet"
    if not tasks_parquet.exists():
        return
    try:
        import jsonlines  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        return

    table = pq.read_table(tasks_parquet)
    rows = table.to_pylist()
    out_path = dataset_root / "meta" / "tasks.jsonl"
    with jsonlines.open(out_path, mode="w") as writer:
        for row in rows:
            task_name = row.get("task") or row.get("__index_level_0__") or ""
            writer.write({"task_index": int(row.get("task_index", 0)), "task": str(task_name)})


def _split_v30_data_into_v21_files(dataset_root: Path, episode_records: list[dict[str, Any]]) -> None:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"缺少 pyarrow，无法拆分 v3.0 parquet: {exc}") from exc

    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for record in episode_records:
        group_key = (int(record.get("data/chunk_index", 0)), int(record.get("data/file_index", 0)))
        groups.setdefault(group_key, []).append(record)

    for (chunk_index, file_index), records in groups.items():
        source = dataset_root / "data" / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.parquet"
        if not source.exists():
            raise RuntimeError(f"缺少 v3.0 数据 parquet: {source}")
        table = pq.read_table(source)
        records = sorted(records, key=lambda item: int(item.get("dataset_from_index", 0)))
        file_offset = int(records[0].get("dataset_from_index", 0))
        for record in records:
            episode_index = int(record.get("episode_index", 0))
            start = int(record.get("dataset_from_index", 0)) - file_offset
            stop = int(record.get("dataset_to_index", 0)) - file_offset
            dest = dataset_root / "data" / f"chunk-{episode_index // 1000:03d}" / f"episode_{episode_index:06d}.parquet"
            dest.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table.slice(start, max(0, stop - start)), dest, compression="snappy")


def _write_legacy_episode_metadata(dataset_root: Path, episode_records: list[dict[str, Any]]) -> None:
    try:
        import jsonlines  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"缺少 jsonlines，无法写入 v2.1 metadata: {exc}") from exc

    episodes_path = dataset_root / "meta" / "episodes.jsonl"
    stats_path = dataset_root / "meta" / "episodes_stats.jsonl"
    with jsonlines.open(episodes_path, mode="w") as episode_writer, jsonlines.open(stats_path, mode="w") as stats_writer:
        for record in sorted(episode_records, key=lambda item: int(item.get("episode_index", 0))):
            episode_payload = {
                key: _json_safe(value)
                for key, value in record.items()
                if not key.startswith("data/")
                and not key.startswith("videos/")
                and not key.startswith("stats/")
                and not key.startswith("meta/")
                and key not in {"dataset_from_index", "dataset_to_index"}
            }
            episode_writer.write(episode_payload)

            stats_payload = _nested_stats_from_record(record)
            stats_writer.write(
                {
                    "episode_index": int(record.get("episode_index", 0)),
                    "stats": _json_safe(stats_payload),
                }
            )


def _copy_or_split_v30_videos(dataset_root: Path, episode_records: list[dict[str, Any]], video_keys: list[str]) -> None:
    for video_key in video_keys:
        groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
        chunk_key = f"videos/{video_key}/chunk_index"
        file_key = f"videos/{video_key}/file_index"
        for record in episode_records:
            if chunk_key not in record or file_key not in record:
                continue
            group_id = (int(record.get(chunk_key, 0)), int(record.get(file_key, 0)))
            groups.setdefault(group_id, []).append(record)

        for (chunk_index, file_index), records in groups.items():
            source = dataset_root / "videos" / video_key / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.mp4"
            if not source.exists():
                continue
            if len(records) != 1:
                continue
            episode_index = int(records[0].get("episode_index", 0))
            dest = dataset_root / "videos" / f"chunk-{episode_index // 1000:03d}" / video_key / f"episode_{episode_index:06d}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(source, dest)


def _nested_stats_from_record(record: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    keep_keys = {"mean", "std", "min", "max", "count"}
    for key, value in record.items():
        if not key.startswith("stats/"):
            continue
        parts = key.split("/")
        stat_name = parts[-1]
        if stat_name not in keep_keys:
            continue
        feature_name = "/".join(parts[1:-1])
        feature_stats = nested.setdefault(feature_name, {})
        feature_stats[stat_name] = value
    return nested


def _json_safe(value: Any) -> Any:
    try:
        import numpy as np  # type: ignore
    except Exception:
        np = None  # type: ignore
    if np is not None:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value



def _build_v21_image_feature_specs() -> dict[str, Any]:
    return {
        "observation.images.hand_left_color": {
            "dtype": "image",
            "shape": [480, 848, 3],
            "names": ["height", "width", "channels"],
        },
        "observation.images.hand_right_color": {
            "dtype": "image",
            "shape": [480, 848, 3],
            "names": ["height", "width", "channels"],
        },
        "observation.images.head_color": {
            "dtype": "image",
            "shape": [720, 1280, 3],
            "names": ["height", "width", "channels"],
        },
    }


def _build_v21_compat_feature_map(existing_features: dict[str, Any], selected_video_keys: list[str]) -> dict[str, Any]:
    compat = dict(existing_features)
    image_specs = _build_v21_image_feature_specs()
    for video_key in selected_video_keys:
        image_column = _build_v21_image_struct_column_name(video_key)
        compat[image_column] = image_specs[image_column]

    compat.setdefault("observation.state", {"dtype": "float32", "shape": [16], "names": ["joint_positions"]})
    compat.setdefault("actions", {"dtype": "float32", "shape": [16], "names": ["joint_actions"]})
    compat.setdefault("timestamp", {"dtype": "float32", "shape": [1], "names": None})
    compat.setdefault("frame_index", {"dtype": "int64", "shape": [1], "names": None})
    compat.setdefault("episode_index", {"dtype": "int64", "shape": [1], "names": None})
    compat.setdefault("index", {"dtype": "int64", "shape": [1], "names": None})
    compat.setdefault("task_index", {"dtype": "int64", "shape": [1], "names": None})
    return compat


def _rewrite_v21_episode_parquet_schema(
    dataset_root: Path,
    raw_source_dir: Path | None = None,
    *,
    preserve_original_columns: bool = False,
) -> None:
    info_path = dataset_root / "meta" / "info.json"
    info = _load_info_json(info_path)
    video_keys = _discover_v21_video_keys(dataset_root, info)
    data_dir = dataset_root / "data"
    parquet_files = sorted(data_dir.rglob("episode_*.parquet")) if data_dir.exists() else []
    if not parquet_files:
        return

    for parquet_path in parquet_files:
        _rewrite_v21_episode_parquet_file(
            dataset_root,
            parquet_path,
            video_keys,
            str(info.get("video_path") or ""),
            raw_source_dir=raw_source_dir,
            preserve_original_columns=preserve_original_columns,
        )

    info["robot_type"] = "agibot"
    if preserve_original_columns:
        info["features"] = _build_v21_compat_feature_map(
            info.get("features", {}) if isinstance(info.get("features", {}), dict) else {},
            video_keys,
        )
    else:
        info["features"] = {
            **_build_v21_image_feature_specs(),
            "observation.state": {"dtype": "float32", "shape": [16], "names": ["joint_positions"]},
            "actions": {"dtype": "float32", "shape": [16], "names": ["joint_actions"]},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
        }
    info["total_videos"] = 0
    info.pop("video_embedding", None)
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8")


def _discover_v21_video_keys(dataset_root: Path, info: dict[str, Any]) -> list[str]:
    feature_map = info.get("features", {})
    keys: list[str] = []
    if isinstance(feature_map, dict):
        keys.extend(
            key
            for key in _EMBED_VIDEO_KEYS
            if isinstance(feature_map.get(key), dict) and feature_map[key].get("dtype") == "video"
        )
    if keys:
        return keys

    videos_root = dataset_root / "videos"
    for key in _EMBED_VIDEO_KEYS:
        if any(videos_root.rglob(f"{key}/episode_*.mp4")) or any(videos_root.rglob(f"{key}/file-*.mp4")):
            keys.append(key)
    return keys


def _rewrite_v21_episode_parquet_file(
    dataset_root: Path,
    parquet_path: Path,
    selected_keys: list[str],
    video_path_template: str,
    *,
    raw_source_dir: Path | None = None,
    preserve_original_columns: bool = False,
) -> None:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"缺少 pyarrow，无法重写 v2.1 parquet schema: {exc}") from exc

    table = pq.read_table(parquet_path)
    if table.num_rows <= 0:
        return

    state_source = "observation.state" if "observation.state" in table.column_names else "observation.states.joint.position"
    if state_source not in table.column_names:
        raise RuntimeError(f"v2.1 parquet 缺少状态列，无法重写 schema: {parquet_path}")
    action_source = "actions" if "actions" in table.column_names else "actions.joint.position"

    state_rows = _normalize_v21_vector_rows(table.column(state_source).to_pylist(), "observation.state")
    action_rows = (
        _normalize_v21_vector_rows(table.column(action_source).to_pylist(), "actions")
        if action_source in table.column_names
        else [list(row) for row in state_rows]
    )
    frame_indices = _get_column_or_default(table, "frame_index", list(range(table.num_rows)))
    episode_indices = _get_column_or_default(table, "episode_index", [0] * table.num_rows)
    episode_index = int(episode_indices[0]) if episode_indices else 0

    ordered_keys = [key for key in _V21_IMAGE_COLUMN_ORDER if key in selected_keys]
    ordered_keys.extend(key for key in selected_keys if key not in ordered_keys)

    image_columns: list[tuple[str, pa.Array]] = []
    for video_key in ordered_keys:
        column_name = _build_v21_image_struct_column_name(video_key)
        image_rows = _read_v21_image_rows(
            dataset_root=dataset_root,
            parquet_path=parquet_path,
            table=table,
            video_key=video_key,
            video_path_template=video_path_template,
            camera_name=column_name.removeprefix("observation.images."),
            episode_index=episode_index,
            frame_indices=frame_indices,
            target_count=table.num_rows,
            raw_source_dir=raw_source_dir,
        )
        image_columns.append(
            (
                column_name,
                pa.array(
                    image_rows,
                    type=pa.struct([
                        pa.field("bytes", pa.binary()),
                        pa.field("path", pa.string()),
                    ]),
                ),
            )
        )

    if preserve_original_columns:
        rewritten = table
        for name, column in image_columns:
            rewritten = _replace_or_append_column(rewritten, name, column)

        rewritten = _replace_or_append_column(
            rewritten,
            "observation.state",
            pa.array(state_rows, type=pa.list_(pa.float32(), 16)),
        )
        rewritten = _replace_or_append_column(
            rewritten,
            "actions",
            pa.array(action_rows, type=pa.list_(pa.float32(), 16)),
        )
    else:
        columns: list[pa.Array] = []
        names: list[str] = []
        for name, column in image_columns:
            names.append(name)
            columns.append(column)
        names.append("observation.state")
        columns.append(pa.array(state_rows, type=pa.list_(pa.float32(), 16)))
        names.append("actions")
        columns.append(pa.array(action_rows, type=pa.list_(pa.float32(), 16)))
        for name in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
            if name in table.column_names:
                names.append(name)
                columns.append(table.column(name).combine_chunks())
        rewritten = pa.table(columns, names=names)
    rewritten = _apply_hf_image_feature_schema(rewritten, [name for name, _ in image_columns])
    pq.write_table(rewritten, parquet_path, compression="snappy")


def _apply_hf_image_feature_schema(table, image_column_names: list[str]):
    if not image_column_names:
        return table

    try:
        import pyarrow as pa  # type: ignore
        from datasets import Features, Image  # type: ignore
    except Exception:
        return table

    features = Features.from_arrow_schema(table.schema)
    changed = False
    for column_name in image_column_names:
        if column_name not in table.column_names:
            continue
        features[column_name] = Image()
        changed = True

    if not changed:
        return table

    target_schema = features.arrow_schema
    arrays = [table.column(name).combine_chunks() for name in table.column_names]
    return pa.Table.from_arrays(arrays, schema=target_schema)


def _normalize_v21_vector_rows(values: list[Any], column_name: str) -> list[list[float]]:
    rows: list[list[float]] = []
    for row in values:
        if row is None:
            raise RuntimeError(f"{column_name} 存在空值，无法写入兼容列")
        normalized = [float(item) for item in row]
        if len(normalized) != 16:
            raise RuntimeError(f"{column_name} 期望 16 维，实际为 {len(normalized)}")
        rows.append(normalized)
    return rows


def _replace_or_append_column(table, column_name: str, column):
    if column_name in table.column_names:
        table = table.remove_column(table.column_names.index(column_name))
    return table.append_column(column_name, column)


def _get_column_or_default(table, column_name: str, default: list[Any]) -> list[Any]:
    if column_name not in table.column_names:
        return list(default)
    return table.column(column_name).to_pylist()


def _build_v21_image_struct_column_name(video_key: str) -> str:
    suffix = video_key.removeprefix("observation.images.")
    if suffix.endswith("_color"):
        return f"observation.images.{suffix}"
    return f"observation.images.{suffix}_color"


def _read_video_frames_as_image_structs(
    video_path: Path,
    *,
    camera_name: str,
    episode_index: int,
    frame_indices: list[Any],
    target_count: int,
) -> list[dict[str, Any]]:
    encoded_frames = _read_video_frames_as_jpeg_bytes(video_path)
    if not encoded_frames:
        raise RuntimeError(f"视频文件没有可用帧: {video_path}")
    if len(encoded_frames) != target_count:
        encoded_frames = _resample_embedded_video_frames(encoded_frames, target_count)
    normalized_frame_indices = [int(value or 0) for value in frame_indices]
    if len(normalized_frame_indices) != target_count:
        normalized_frame_indices = list(range(target_count))
    return [
        {
            "bytes": payload,
            "path": f"frame_{frame_index:06d}.jpg",
        }
        for payload, frame_index in zip(encoded_frames, normalized_frame_indices)
    ]


def _read_v21_image_rows(
    *,
    dataset_root: Path,
    parquet_path: Path,
    table,
    video_key: str,
    video_path_template: str,
    camera_name: str,
    episode_index: int,
    frame_indices: list[Any],
    target_count: int,
    raw_source_dir: Path | None,
) -> list[dict[str, Any]]:
    raw_rows = _read_raw_frames_as_image_structs(
        raw_source_dir=raw_source_dir,
        video_key=video_key,
        episode_index=episode_index,
        target_count=target_count,
    )
    if raw_rows is not None:
        return raw_rows
    return _read_video_frames_as_image_structs(
        _resolve_video_path_for_parquet(dataset_root, parquet_path, video_key, video_path_template, table),
        camera_name=camera_name,
        episode_index=episode_index,
        frame_indices=frame_indices,
        target_count=target_count,
    )


def _read_raw_frames_as_image_structs(
    *,
    raw_source_dir: Path | None,
    video_key: str,
    episode_index: int,
    target_count: int,
) -> list[dict[str, Any]] | None:
    if raw_source_dir is None:
        return None

    episode_dir = _resolve_raw_episode_dir(raw_source_dir, episode_index)
    if episode_dir is None:
        return None

    camera_dir_name = _RAW_IMAGE_CAMERA_DIRS.get(video_key)
    if camera_dir_name is None:
        return None

    color_dir = episode_dir / 'camera' / camera_dir_name / 'color'
    image_paths = _sorted_raw_image_paths(color_dir)
    extracted_dir: Path | None = None
    if not image_paths:
        extracted_dir = _extract_raw_video_frames_to_temp_dir(episode_dir, video_key)
        if extracted_dir is not None:
            image_paths = _sorted_raw_image_paths(extracted_dir)
    if not image_paths:
        return None

    try:
        rows = [
            {
                'bytes': _encode_raw_image_file_to_jpeg_bytes(path, camera_dir_name),
                'path': f'frame_{int(path.stem):06d}.jpg',
            }
            for path in image_paths
        ]
    finally:
        if extracted_dir is not None:
            shutil.rmtree(extracted_dir, ignore_errors=True)

    if len(rows) != target_count:
        rows = _resample_embedded_video_frames(rows, target_count)
    return rows


def _resolve_raw_episode_dir(raw_source_dir: Path, episode_index: int) -> Path | None:
    candidates = [raw_source_dir]
    try:
        candidates.extend(sorted((p for p in raw_source_dir.iterdir() if p.is_dir()), key=lambda item: item.name))
    except OSError:
        pass

    exact = {str(episode_index), f'{episode_index:06d}', str(episode_index + 1), f'{episode_index + 1:06d}'}
    for candidate in candidates:
        if _has_raw_visual_source(candidate) and ((candidate / 'record').is_dir() or candidate.name in exact):
            return candidate

    numeric_dirs = [
        path
        for path in candidates
        if path != raw_source_dir and path.is_dir() and path.name.isdigit() and _has_raw_visual_source(path)
    ]
    if len(numeric_dirs) == 1:
        return numeric_dirs[0]

    return None


def _has_raw_visual_source(path: Path) -> bool:
    if (path / 'camera').is_dir():
        return True
    return any((path / name).is_file() for name in _RAW_VIDEO_FILE_NAMES.values())


def _extract_raw_video_frames_to_temp_dir(episode_dir: Path, video_key: str) -> Path | None:
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise RuntimeError(f'缺少 cv2，无法从原始视频拆帧: {exc}') from exc

    video_name = _RAW_VIDEO_FILE_NAMES.get(video_key)
    if video_name is None:
        return None

    video_path = episode_dir / video_name
    if not video_path.exists():
        return None

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f'无法打开原始视频文件进行拆帧: {video_path}')

    temp_dir = _short_temp_root('raw_frame_extract') / f"{episode_dir.name}_{video_path.stem}_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    frame_index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frame_path = temp_dir / f'{frame_index:08d}.jpg'
            if not cv2.imwrite(str(frame_path), frame):
                raise RuntimeError(f'原始视频拆帧写入失败: {frame_path}')
            frame_index += 1
    finally:
        capture.release()

    if frame_index == 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    return temp_dir


def _sorted_raw_image_paths(color_dir: Path) -> list[Path]:
    if not color_dir.is_dir():
        return []

    def _sort_key(path: Path) -> tuple[int, str]:
        try:
            return (int(path.stem), path.name)
        except ValueError:
            return (sys.maxsize, path.name)

    return sorted(
        [path for path in color_dir.iterdir() if path.is_file() and path.suffix.lower() in {'.jpg', '.jpeg', '.png'}],
        key=_sort_key,
    )


def _encode_raw_image_file_to_jpeg_bytes(image_path: Path, camera_dir_name: str) -> bytes:
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise RuntimeError(f'缺少 cv2，无法读取原始图片 {image_path}: {exc}') from exc

    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f'缺少 Pillow，无法编码原始图片 {image_path}: {exc}') from exc

    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f'无法读取原始图片文件: {image_path}')

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    resized = _resize_raw_image(rgb, camera_dir_name)
    buffer = BytesIO()
    Image.fromarray(resized).save(buffer, format='JPEG', quality=90)
    return buffer.getvalue()


def _resize_raw_image(image, camera_dir_name: str):
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise RuntimeError(f'缺少 cv2，无法缩放原始图片: {exc}') from exc

    camera_type = 'head' if 'head' in camera_dir_name else 'hand'
    target_h, target_w = (720, 1280) if camera_type == 'head' else (480, 848)
    if image.shape[0] == target_h and image.shape[1] == target_w:
        return image
    if image.ndim == 3:
        return cv2.resize(image, (target_w, target_h), interpolation=cv2.INTER_AREA)
    return cv2.resize(image, (target_w, target_h), interpolation=cv2.INTER_NEAREST)


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


def _cleanup_v30_artifacts_from_v21_layout(dataset_root: Path) -> None:
    cleanup_patterns = [
        "data/chunk-*/file-*.parquet",
        "meta/episodes/chunk-*/file-*.parquet",
        "meta/tasks*.parquet",
        "meta/episodes_stats/chunk-*/file-*.parquet",
        "videos/*/chunk-*/file-*.mp4",
    ]
    for pattern in cleanup_patterns:
        for path in dataset_root.glob(pattern):
            if path.is_file():
                path.unlink(missing_ok=True)

    empty_dirs = [
        dataset_root / "meta" / "episodes",
        dataset_root / "meta" / "episodes_stats",
        dataset_root / "videos",
        dataset_root / "data",
    ]
    for base in empty_dirs:
        _remove_empty_directories(base, stop_at=dataset_root)


def _remove_empty_directories(base: Path, *, stop_at: Path) -> None:
    if not base.exists():
        return

    for path in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue

    current = base
    while current.exists() and current != stop_at:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


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
            errors.extend(_validate_v21_required_schema(root, parquet_files, info))
        elif version == "v2.0":
            if not (root / "meta" / "stats.json").exists():
                errors.append(f"{root}: 缺少 v2.0 必需文件 meta/stats.json")
        else:
            errors.append(f"{root}: 不支持校验的版本 {version!r}")

    if errors:
        raise RuntimeError("输出校验失败:\n- " + "\n- ".join(errors))


def _validate_v21_required_schema(root: Path, parquet_files: list[Path], info: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not parquet_files:
        return errors

    required_features = {
        "observation.state",
        "actions",
        "observation.images.hand_left_color",
        "observation.images.hand_right_color",
        "observation.images.head_color",
        "timestamp",
        "frame_index",
        "episode_index",
        "index",
        "task_index",
    }

    feature_map = info.get("features", {})
    if isinstance(feature_map, dict):
        missing_features = sorted(required_features - set(feature_map.keys()))
        if missing_features:
            errors.append(f"{root}: v2.1 info.json 缺少目标 schema 特征 {missing_features}")

    sample_path = sorted(parquet_files)[0]
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:
        errors.append(f"{root}: 缺少 pyarrow，无法校验 v2.1 parquet schema: {exc}")
        return errors

    try:
        table = pq.read_table(sample_path)
    except Exception as exc:
        errors.append(f"{root}: 读取 v2.1 parquet 失败 {sample_path}: {exc}")
        return errors

    missing_columns = sorted(required_features - set(table.column_names))
    if missing_columns:
        errors.append(f"{root}: v2.1 parquet 缺少目标列 {missing_columns}")
    return errors


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


def _embed_videos_in_parquet(output_dir: Path, raw_source_dir: Path | None = None) -> None:
    for root in _iter_dataset_roots(output_dir):
        _embed_dataset_videos_in_parquet(root, raw_source_dir=raw_source_dir)


def _embed_dataset_videos_in_parquet(dataset_root: Path, raw_source_dir: Path | None = None) -> None:
    info_path = dataset_root / "meta" / "info.json"
    info = _load_info_json(info_path)
    if str(info.get("codebase_version", "")).strip() == "v2.1":
        _rewrite_v21_episode_parquet_schema(dataset_root, raw_source_dir=raw_source_dir)
        return

    feature_map = info.get("features", {})
    if not isinstance(feature_map, dict):
        return

    selected_keys = [
        key
        for key in _EMBED_VIDEO_KEYS
        if isinstance(feature_map.get(key), dict) and feature_map[key].get("dtype") == "video"
    ]
    if not selected_keys:
        return

    data_dir = dataset_root / "data"
    parquet_files = sorted(data_dir.rglob("*.parquet")) if data_dir.exists() else []
    if not parquet_files:
        raise RuntimeError(f"缺少可嵌入视频的数据 parquet: {data_dir}")

    for parquet_path in parquet_files:
        _rewrite_v21_episode_parquet_file(
            dataset_root,
            parquet_path,
            selected_keys,
            str(info.get("video_path") or ""),
            raw_source_dir=raw_source_dir,
            preserve_original_columns=True,
        )

    info["features"] = _build_v21_compat_feature_map(feature_map, selected_keys)
    info["video_embedding"] = {
        "enabled": True,
        "encoding": "jpeg_struct",
        "keys": selected_keys,
    }
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=4), encoding="utf-8")


def _embedded_video_column_name(video_key: str) -> str:
    suffix = video_key.removeprefix("observation.images.")
    return f"observation.frames.{suffix}"


def _resolve_video_path_for_parquet(
    dataset_root: Path,
    parquet_path: Path,
    video_key: str,
    video_path_template: str,
    table,
) -> Path:
    chunk_match = re.search(r"chunk-(\d+)$", parquet_path.parent.name)
    file_match = re.search(r"file-(\d+)\.parquet$", parquet_path.name)
    episode_match = re.search(r"episode_(\d+)\.parquet$", parquet_path.name)
    chunk_index = int(chunk_match.group(1)) if chunk_match else 0
    file_index = int(file_match.group(1)) if file_match else 0
    episode_index = int(episode_match.group(1)) if episode_match else None
    if episode_index is None and "episode_index" in table.column_names and table.num_rows > 0:
        episode_index = int(table.column("episode_index")[0].as_py())
    if episode_index is None:
        episode_index = 0

    values = {
        "video_key": video_key,
        "chunk_index": chunk_index,
        "file_index": file_index,
        "episode_chunk": chunk_index,
        "episode_index": episode_index,
    }
    if video_path_template:
        candidate = dataset_root / video_path_template.format(**values)
        if candidate.exists():
            return candidate

    fallback_v30 = dataset_root / "videos" / video_key / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.mp4"
    if fallback_v30.exists():
        return fallback_v30
    fallback_v21 = dataset_root / "videos" / f"chunk-{chunk_index:03d}" / video_key / f"episode_{episode_index:06d}.mp4"
    if fallback_v21.exists():
        return fallback_v21
    raise RuntimeError(f"缺少待嵌入视频文件: parquet={parquet_path}, video_key={video_key}")


def _resample_embedded_video_frames(frames: list[bytes], target_count: int) -> list[bytes]:
    if target_count <= 0:
        return []
    if not frames:
        return []
    if len(frames) == target_count:
        return frames
    if len(frames) == 1:
        return [frames[0]] * target_count

    last_src = len(frames) - 1
    last_dst = max(1, target_count - 1)
    return [frames[round(index * last_src / last_dst)] for index in range(target_count)]


def _read_video_frames_as_jpeg_bytes(video_path: Path) -> list[bytes]:
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"缺少 cv2，无法解码视频 {video_path}: {exc}") from exc

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频文件: {video_path}")

    frames: list[bytes] = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            encoded_ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if not encoded_ok:
                raise RuntimeError(f"JPEG 编码失败: {video_path}")
            frames.append(bytes(buffer))
    finally:
        capture.release()
    return frames


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
    with _STDIO_GUARD_LOCK:
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
    stale_agibotworld = dst / "agibotworld"
    if stale_agibotworld.exists() and not (src / "agibotworld").exists():
        shutil.rmtree(stale_agibotworld, ignore_errors=True)
    for item in src.iterdir():
        target = dst / item.name
        if not target.exists():
            shutil.move(str(item), str(target))
            continue
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
