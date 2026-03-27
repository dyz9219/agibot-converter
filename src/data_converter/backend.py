from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime
import json
import multiprocessing
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .converters.lerobot_runner import run_lerobot_task
from .converters.rosbag_runner import run_rosbag_task
from .manifest import write_manifest
from .models import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, ConversionOptions, PrecheckResult, TargetKind, TaskPlan, TaskStatus
from .precheck import run_precheck
from .process_tracker import register_child, unregister_child


ProgressCallback = Callable[[TaskPlan], None]
BoolCallback = Callable[[], bool]


@dataclass(slots=True)
class RunSummary:
    total: int
    success: int
    failed: int
    skipped: int


class ConversionBackend:
    def precheck(self, options: ConversionOptions) -> PrecheckResult:
        return run_precheck(options)

    def run(
        self,
        options: ConversionOptions,
        plans: list[TaskPlan],
        on_progress: ProgressCallback | None = None,
        should_pause: BoolCallback | None = None,
        should_cancel: BoolCallback | None = None,
    ) -> RunSummary:
        runnable = [p for p in plans if p.status is TaskStatus.PENDING]
        skipped = len([p for p in plans if p.status is TaskStatus.SKIPPED])
        success = 0
        failed = 0

        if not runnable:
            return RunSummary(total=len(plans), success=0, failed=0, skipped=skipped)

        if _should_use_lerobot_worker_shards(options, runnable):
            return _run_lerobot_worker_shards(
                options,
                runnable,
                total=len(plans),
                skipped=skipped,
                on_progress=on_progress,
            )

        max_workers = self._configure_parallelism(options, runnable)

        if _should_use_lerobot_worker_process_pool(options, runnable):
            return _run_lerobot_worker_process_pool(
                options,
                runnable,
                total=len(plans),
                skipped=skipped,
                max_workers=max_workers,
                on_progress=on_progress,
            )

        pending = list(runnable)
        cancelled = False
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures: dict[Future[None], TaskPlan] = {}
            start_times: dict[Future[None], float] = {}
            while pending or futures:
                if should_cancel is not None and should_cancel():
                    cancelled = True

                while pending and len(futures) < max_workers and not cancelled:
                    if should_pause is not None and should_pause():
                        break
                    task = pending.pop(0)
                    task.status = TaskStatus.RUNNING
                    task.started_at = datetime.now().astimezone().isoformat()
                    task.finished_at = ""
                    task.elapsed_seconds = None
                    if on_progress is not None:
                        on_progress(task)
                    future = pool.submit(self._run_task, task, options)
                    futures[future] = task
                    start_times[future] = time.perf_counter()

                if not futures:
                    if cancelled:
                        break
                    time.sleep(0.15)
                    continue

                done, _ = wait(list(futures.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    task = futures.pop(future)
                    started = start_times.pop(future, None)
                    finished_at = datetime.now().astimezone().isoformat()
                    task.finished_at = finished_at
                    if started is not None:
                        task.elapsed_seconds = round(time.perf_counter() - started, 6)
                    try:
                        future.result()
                        task.status = TaskStatus.SUCCESS
                        success += 1
                        write_manifest(task, options, status=TaskStatus.SUCCESS.value)
                    except Exception as exc:  # noqa: BLE001
                        task.status = TaskStatus.FAILED
                        task.reasons.append(str(exc))
                        details = getattr(exc, "issues", None)
                        if isinstance(details, list):
                            task.error_details = [str(x) for x in details]
                        if options.target is TargetKind.LEROBOT:
                            _cleanup_lerobot_partial_outputs(task.output_dir)
                        failed += 1
                        write_manifest(task, options, status=TaskStatus.FAILED.value, error=str(exc))
                    if on_progress is not None:
                        on_progress(task)

            if cancelled:
                for task in pending:
                    task.status = TaskStatus.BLOCKED
                    task.reasons.append("任务已取消")
                    if on_progress is not None:
                        on_progress(task)

        return RunSummary(total=len(plans), success=success, failed=failed, skipped=skipped)

    def _run_task(self, plan: TaskPlan, options: ConversionOptions) -> None:
        plan.status = TaskStatus.RUNNING
        max_retries = 0 if options.target is TargetKind.LEROBOT else options.retry_limit
        first_exc: Exception | None = None
        while True:
            try:
                if options.target is TargetKind.LEROBOT:
                    run_lerobot_task(plan, options)
                else:
                    run_rosbag_task(plan, options)
                return
            except Exception as exc:  # noqa: BLE001
                if first_exc is None:
                    first_exc = exc
                plan.attempts += 1
                if plan.attempts > max_retries:
                    if first_exc is not None and first_exc is not exc:
                        raise RuntimeError(f"首次失败: {first_exc}\n重试失败: {exc}") from exc
                    raise
                if options.target is TargetKind.ROSBAG:
                    _cleanup_rosbag_partial_outputs(plan.output_dir)

    def _configure_parallelism(self, options: ConversionOptions, runnable: list[TaskPlan]) -> int:
        requested = max(1, options.concurrency)
        for plan in runnable:
            plan.lerobot_inner_concurrency = 1
            plan.lerobot_inprocess_allowed = False

        if options.target is not TargetKind.LEROBOT or options.lerobot_version == "HDF5":
            return requested

        # If there is only one any4 dataset containing multiple task_info entries,
        # prefer inner any4 parallelism and avoid oversubscribing outer workers.
        if len(runnable) == 1:
            estimated_any4_tasks = _estimate_any4_task_count(runnable[0].source)
            runnable[0].lerobot_inprocess_allowed = True
            if estimated_any4_tasks > 1:
                runnable[0].lerobot_inner_concurrency = min(requested, estimated_any4_tasks)
                return 1

        return requested


def _should_use_lerobot_worker_shards(options: ConversionOptions, runnable: list[TaskPlan]) -> bool:
    if options.worker_mode:
        return False
    if options.target is not TargetKind.LEROBOT or options.lerobot_version == "HDF5":
        return False
    if len(runnable) <= 1:
        return False
    return options.concurrency >= DEFAULT_CONCURRENCY * 2


def _should_use_lerobot_worker_process_pool(options: ConversionOptions, runnable: list[TaskPlan]) -> bool:
    if not options.worker_mode:
        return False
    if options.target is not TargetKind.LEROBOT or options.lerobot_version == "HDF5":
        return False
    return len(runnable) > 0


def _normalized_worker_concurrency(requested: int) -> int:
    requested = max(DEFAULT_CONCURRENCY, min(MAX_CONCURRENCY, requested))
    return max(DEFAULT_CONCURRENCY, requested - (requested % DEFAULT_CONCURRENCY))


def _partition_tasks_evenly(plans: list[TaskPlan], worker_count: int) -> list[list[TaskPlan]]:
    worker_count = max(1, worker_count)
    shards: list[list[TaskPlan]] = [[] for _ in range(worker_count)]
    for index, plan in enumerate(plans):
        shards[index % worker_count].append(plan)
    return [shard for shard in shards if shard]


def _run_lerobot_worker_shards(
    options: ConversionOptions,
    runnable: list[TaskPlan],
    *,
    total: int,
    skipped: int,
    on_progress: ProgressCallback | None,
) -> RunSummary:
    normalized = _normalized_worker_concurrency(options.concurrency)
    worker_count = max(1, normalized // DEFAULT_CONCURRENCY)
    shards = _partition_tasks_evenly(runnable, worker_count)
    success = 0
    failed = 0

    for worker_index, shard in enumerate(shards):
        for task in shard:
            task.worker_index = worker_index

    with ThreadPoolExecutor(max_workers=len(shards)) as pool:
        future_map = {
            pool.submit(
                _run_lerobot_worker_shard,
                options,
                worker_index,
                shard,
                on_progress=on_progress,
            ): (worker_index, shard)
            for worker_index, shard in enumerate(shards)
        }
        for future in future_map:
            worker_index, shard = future_map[future]
            try:
                result = future.result()
                success += int(result.get("success", 0))
                failed += int(result.get("failed", 0))
            except Exception as exc:  # noqa: BLE001
                for task in shard:
                    task.status = TaskStatus.FAILED
                    task.reasons.append(str(exc))
                    task.finished_at = datetime.now().astimezone().isoformat()
                    if task.started_at and task.elapsed_seconds is None:
                        task.elapsed_seconds = 0.0
                    write_manifest(task, options, status=TaskStatus.FAILED.value, error=str(exc))
                    if on_progress is not None:
                        on_progress(task)
                failed += len(shard)
    return RunSummary(total=total, success=success, failed=failed, skipped=skipped)


def _run_lerobot_worker_process_pool(
    options: ConversionOptions,
    runnable: list[TaskPlan],
    *,
    total: int,
    skipped: int,
    max_workers: int,
    on_progress: ProgressCallback | None,
) -> RunSummary:
    success = 0
    failed = 0
    pending = list(runnable)
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=context,
        initializer=_initialize_lerobot_worker_process,
    ) as pool:
        futures: dict[Future[dict], TaskPlan] = {}
        while pending or futures:
            while pending and len(futures) < max_workers:
                task = pending.pop(0)
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now().astimezone().isoformat()
                task.finished_at = ""
                task.elapsed_seconds = None
                if on_progress is not None:
                    on_progress(task)
                futures[pool.submit(_run_lerobot_task_in_worker_process, task, options)] = task

            done, _ = wait(list(futures.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                task = futures.pop(future)
                try:
                    payload = future.result()
                    _apply_worker_process_result(task, payload)
                    if task.status is TaskStatus.SUCCESS:
                        success += 1
                    else:
                        if options.target is TargetKind.LEROBOT:
                            _cleanup_lerobot_partial_outputs(task.output_dir)
                        failed += 1
                except Exception as exc:  # noqa: BLE001
                    task.status = TaskStatus.FAILED
                    task.finished_at = datetime.now().astimezone().isoformat()
                    task.reasons.append(str(exc))
                    if options.target is TargetKind.LEROBOT:
                        _cleanup_lerobot_partial_outputs(task.output_dir)
                    write_manifest(task, options, status=TaskStatus.FAILED.value, error=str(exc))
                    failed += 1
                if on_progress is not None:
                    on_progress(task)

    return RunSummary(total=total, success=success, failed=failed, skipped=skipped)


def _run_lerobot_worker_shard(
    options: ConversionOptions,
    worker_index: int,
    shard: list[TaskPlan],
    *,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    payload_path = _write_worker_payload(options, worker_index, shard)
    try:
        for task in shard:
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now().astimezone().isoformat()
            task.finished_at = ""
            task.elapsed_seconds = None
            if on_progress is not None:
                on_progress(task)

        proc = _run_worker_subprocess(payload_path)
        _sync_tasks_from_manifests(shard, options)
        if on_progress is not None:
            for task in shard:
                on_progress(task)
        success = sum(1 for task in shard if task.status is TaskStatus.SUCCESS)
        failed = sum(1 for task in shard if task.status is TaskStatus.FAILED)
        if proc.returncode != 0 and failed == 0:
            raise RuntimeError(f"worker_{worker_index} exited with code {proc.returncode}")
        return {"success": success, "failed": failed}
    finally:
        payload_path.unlink(missing_ok=True)


def _initialize_lerobot_worker_process() -> None:
    from .any4lerobot_bridge import preload_any4_runtime

    preload_any4_runtime()


def _run_lerobot_task_in_worker_process(task: TaskPlan, options: ConversionOptions) -> dict:
    result_task = replace(task)
    result_task.stage_timings = {}
    result_task.reasons = list(task.reasons)
    result_task.error_details = list(task.error_details)
    result_task.lerobot_inner_concurrency = 1
    result_task.lerobot_inprocess_allowed = True
    started_perf = time.perf_counter()
    result_task.started_at = datetime.now().astimezone().isoformat()
    result_task.finished_at = ""
    error: str | None = None
    try:
        run_lerobot_task(result_task, options)
        result_task.status = TaskStatus.SUCCESS
    except Exception as exc:  # noqa: BLE001
        result_task.status = TaskStatus.FAILED
        result_task.reasons.append(str(exc))
        details = getattr(exc, "issues", None)
        if isinstance(details, list):
            result_task.error_details = [str(x) for x in details]
        error = str(exc)
        if options.target is TargetKind.LEROBOT:
            _cleanup_lerobot_partial_outputs(result_task.output_dir)
    result_task.finished_at = datetime.now().astimezone().isoformat()
    result_task.elapsed_seconds = round(time.perf_counter() - started_perf, 6)
    write_manifest(result_task, options, status=result_task.status.value, error=error)
    return {
        "status": result_task.status.value,
        "started_at": result_task.started_at,
        "finished_at": result_task.finished_at,
        "elapsed_seconds": result_task.elapsed_seconds,
        "reasons": result_task.reasons,
        "error_details": result_task.error_details,
        "input_kind": result_task.input_kind,
        "adapter_used": result_task.adapter_used,
        "adapter_workdir": result_task.adapter_workdir,
        "runtime_mode": result_task.runtime_mode,
        "runtime_diagnostic": result_task.runtime_diagnostic,
        "path_strategy": result_task.path_strategy,
        "path_risk_level": result_task.path_risk_level,
        "path_risk_reason": result_task.path_risk_reason,
        "stage_workdir": result_task.stage_workdir,
        "stage_timings": result_task.stage_timings,
    }


def _apply_worker_process_result(task: TaskPlan, payload: dict) -> None:
    status_map = {status.value: status for status in TaskStatus}
    task.status = status_map.get(str(payload.get("status", "")), TaskStatus.FAILED)
    task.started_at = str(payload.get("started_at") or task.started_at)
    task.finished_at = str(payload.get("finished_at") or task.finished_at)
    task.elapsed_seconds = payload.get("elapsed_seconds", task.elapsed_seconds)
    task.reasons = [str(x) for x in payload.get("reasons", task.reasons)]
    task.error_details = [str(x) for x in payload.get("error_details", task.error_details)]
    task.input_kind = str(payload.get("input_kind") or task.input_kind)
    task.adapter_used = bool(payload.get("adapter_used", task.adapter_used))
    task.adapter_workdir = str(payload.get("adapter_workdir") or task.adapter_workdir)
    task.runtime_mode = str(payload.get("runtime_mode") or task.runtime_mode)
    task.runtime_diagnostic = str(payload.get("runtime_diagnostic") or task.runtime_diagnostic)
    task.path_strategy = str(payload.get("path_strategy") or task.path_strategy)
    task.path_risk_level = str(payload.get("path_risk_level") or task.path_risk_level)
    task.path_risk_reason = str(payload.get("path_risk_reason") or task.path_risk_reason)
    task.stage_workdir = str(payload.get("stage_workdir") or task.stage_workdir)
    task.stage_timings = {
        str(k): float(v) for k, v in (payload.get("stage_timings") or {}).items()
    }


def _write_worker_payload(options: ConversionOptions, worker_index: int, shard: list[TaskPlan]) -> Path:
    root = options.output_path / ".worker-payloads"
    root.mkdir(parents=True, exist_ok=True)
    payload_path = root / f"worker_{worker_index:02d}.json"
    worker_summary_dir = options.output_path / ".worker-results"
    worker_summary_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "options": {
            "input_path": str(options.input_path),
            "output_path": str(options.output_path),
            "target": options.target.value,
            "lerobot_version": options.lerobot_version,
            "fps": options.fps,
            "bag_type": options.bag_type,
            "concurrency": DEFAULT_CONCURRENCY,
            "conflict_policy": options.conflict_policy,
            "retry_limit": options.retry_limit,
            "worker_mode": True,
            "embed_videos_in_parquet": options.embed_videos_in_parquet,
        },
        "worker": {
            "worker_index": worker_index,
            "summary_path": str(worker_summary_dir / f"worker_{worker_index:02d}.json"),
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "source": {
                    "name": task.source.name,
                    "source_path": str(task.source.source_path),
                    "is_zip": task.source.is_zip,
                },
                "output_dir": str(task.output_dir),
                "status": task.status.value,
                "worker_index": worker_index,
            }
            for task in shard
        ],
    }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload_path


def _run_worker_subprocess(payload_path: Path) -> subprocess.CompletedProcess[str]:
    cmd = _build_worker_subprocess_cmd(
        payload_path,
        frozen=getattr(os.sys, "frozen", False),
        executable=Path(os.sys.executable),
    )
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    register_child(proc.pid)
    try:
        stdout, stderr = proc.communicate()
    finally:
        unregister_child(proc.pid)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _build_worker_subprocess_cmd(payload_path: Path, *, frozen: bool, executable: Path) -> list[str]:
    if frozen:
        return [str(executable), "--internal-run-worker-shard", "--payload-path", str(payload_path)]
    return [str(executable), "-m", "data_converter.worker_shard_cli", "--payload-path", str(payload_path)]


def _sync_tasks_from_manifests(shard: list[TaskPlan], options: ConversionOptions) -> None:
    status_map = {status.value: status for status in TaskStatus}
    for task in shard:
        manifest_path = task.output_dir / "manifest.json"
        if not manifest_path.exists():
            task.status = TaskStatus.FAILED
            task.reasons.append("worker 未生成 manifest.json")
            write_manifest(task, options, status=TaskStatus.FAILED.value, error="worker 未生成 manifest.json")
            continue
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        task.status = status_map.get(data.get("status", ""), TaskStatus.FAILED)
        task.input_kind = data.get("input_kind") or task.input_kind
        task.adapter_used = bool(data.get("adapter_used", task.adapter_used))
        task.adapter_workdir = data.get("adapter_workdir") or task.adapter_workdir
        task.runtime_mode = data.get("runtime_mode") or task.runtime_mode
        task.runtime_diagnostic = data.get("runtime_diagnostic") or task.runtime_diagnostic
        task.path_strategy = data.get("path_strategy") or task.path_strategy
        task.path_risk_level = data.get("path_risk_level") or task.path_risk_level
        task.path_risk_reason = data.get("path_risk_reason") or task.path_risk_reason
        task.stage_workdir = data.get("stage_workdir") or task.stage_workdir
        task.started_at = data.get("started_at") or task.started_at
        task.finished_at = data.get("finished_at") or task.finished_at
        task.elapsed_seconds = data.get("elapsed_seconds", task.elapsed_seconds)
        task.stage_timings = {
            str(k): float(v) for k, v in (data.get("stage_timings") or {}).items()
        }
        task.error_details = [str(x) for x in data.get("error_details", [])]
        task.reasons = [str(x) for x in data.get("task", {}).get("reasons", task.reasons)]


def _cleanup_rosbag_partial_outputs(output_dir: Path) -> None:
    ros2_output = output_dir / "ros2_output"
    if ros2_output.exists():
        shutil.rmtree(ros2_output, ignore_errors=True)
    ros1_output = output_dir / "ros1_output.bag"
    if ros1_output.exists():
        ros1_output.unlink(missing_ok=True)


def _cleanup_lerobot_partial_outputs(output_dir: Path) -> None:
    for name in ("data", "videos", "meta"):
        p = output_dir / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)


def _estimate_any4_task_count(source) -> int:
    if source.is_zip:
        return 1

    task_info_dir = source.source_path / "task_info"
    if not task_info_dir.is_dir():
        return 1

    try:
        count = sum(1 for p in task_info_dir.glob("*.json") if p.is_file())
    except OSError:
        return 1
    return max(1, count)


def build_options(
    *,
    input_path: str,
    output_path: str,
    target: str,
    version: str,
    fps: str,
    bag_type: str,
    concurrency: str,
    embed_videos_in_parquet: bool | str = False,
) -> ConversionOptions:
    target_kind = TargetKind.LEROBOT if target.lower() == "lerobot" else TargetKind.ROSBAG
    fps_value = int(fps) if fps.strip().isdigit() else 30
    conc_value = int(concurrency) if concurrency.strip().isdigit() else DEFAULT_CONCURRENCY
    return ConversionOptions(
        input_path=Path(input_path),
        output_path=Path(output_path),
        target=target_kind,
        lerobot_version=version,
        fps=fps_value,
        bag_type=bag_type,
        concurrency=max(1, min(MAX_CONCURRENCY, conc_value)),
        embed_videos_in_parquet=(
            embed_videos_in_parquet
            if isinstance(embed_videos_in_parquet, bool)
            else str(embed_videos_in_parquet).strip().lower() in {"1", "true", "yes", "on"}
        ),
    )
