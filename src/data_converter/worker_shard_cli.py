from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

from .backend import ConversionBackend
from .models import ConversionOptions, SourceItem, TargetKind, TaskPlan, TaskStatus


def run_worker_shard_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="data-converter-worker-shard")
    parser.add_argument("--payload-path", required=True)
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.payload_path).read_text(encoding="utf-8"))
    options_data = payload["options"]
    worker_data = payload.get("worker", {})
    target = TargetKind.LEROBOT if options_data["target"] == "lerobot" else TargetKind.ROSBAG
    options = ConversionOptions(
        input_path=Path(options_data["input_path"]),
        output_path=Path(options_data["output_path"]),
        target=target,
        lerobot_version=options_data["lerobot_version"],
        fps=int(options_data["fps"]),
        bag_type=options_data["bag_type"],
        concurrency=int(options_data["concurrency"]),
        conflict_policy=options_data.get("conflict_policy", "block"),
        retry_limit=int(options_data.get("retry_limit", 1)),
        worker_mode=bool(options_data.get("worker_mode", True)),
        embed_videos_in_parquet=bool(options_data.get("embed_videos_in_parquet", False)),
    )
    tasks = []
    for item in payload["tasks"]:
        tasks.append(
            TaskPlan(
                task_id=item["task_id"],
                source=SourceItem(
                    name=item["source"]["name"],
                    source_path=Path(item["source"]["source_path"]),
                    is_zip=bool(item["source"]["is_zip"]),
                ),
                output_dir=Path(item["output_dir"]),
                status=TaskStatus(item.get("status", TaskStatus.PENDING.value)),
                worker_index=int(item.get("worker_index", -1)),
            )
        )

    started_perf = time.perf_counter()
    started_at = datetime.now().astimezone().isoformat()
    summary = ConversionBackend().run(options, tasks, on_progress=None)
    finished_at = datetime.now().astimezone().isoformat()
    elapsed_seconds = round(time.perf_counter() - started_perf, 6)
    summary_path_raw = worker_data.get("summary_path")
    if summary_path_raw:
        summary_path = Path(summary_path_raw)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_payload = {
            "worker_index": int(worker_data.get("worker_index", -1)),
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": elapsed_seconds,
            "task_count": len(tasks),
            "total": summary.total,
            "success": summary.success,
            "failed": summary.failed,
            "skipped": summary.skipped,
        }
        summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "WORKER_SUMMARY",
        f"total={summary.total}",
        f"success={summary.success}",
        f"failed={summary.failed}",
        f"skipped={summary.skipped}",
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_worker_shard_cli(__import__("sys").argv[1:]))
