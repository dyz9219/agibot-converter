from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from data_converter.converters import lerobot_runner


class LerobotTaskTextTests(unittest.TestCase):
    def test_normalizes_task_metadata_to_instruction_text(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"task-text-{uuid.uuid4().hex[:8]}"
        dataset = root / "dataset"
        prefixed = "test_gaok_1_10_174727_93 | 把瓶子放进筐子里"
        expected = "把瓶子放进筐子里"
        try:
            (dataset / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=False)
            (dataset / "data" / "chunk-000").mkdir(parents=True)
            (dataset / "meta" / "info.json").write_text(
                json.dumps({"codebase_version": "v3.0", "total_tasks": 1}),
                encoding="utf-8",
            )
            pq.write_table(
                pa.table(
                    {
                        "task_index": pa.array([0], type=pa.int64()),
                        "__index_level_0__": pa.array([prefixed], type=pa.large_string()),
                    }
                ),
                dataset / "meta" / "tasks.parquet",
            )
            pq.write_table(
                pa.table(
                    {
                        "episode_index": pa.array([0], type=pa.int64()),
                        "tasks": pa.array([[prefixed]], type=pa.list_(pa.string())),
                        "length": pa.array([384], type=pa.int64()),
                    }
                ),
                dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
            )
            (dataset / "meta" / "tasks.jsonl").write_text(
                json.dumps({"task_index": 0, "task": prefixed}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (dataset / "meta" / "episodes.jsonl").write_text(
                json.dumps({"episode_index": 0, "tasks": [prefixed], "length": 384}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )

            lerobot_runner._normalize_lerobot_task_text_metadata(root)

            tasks = pq.read_table(dataset / "meta" / "tasks.parquet").to_pylist()
            episodes = pq.read_table(
                dataset / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
                columns=["tasks"],
            ).to_pylist()
            tasks_jsonl = [
                json.loads(line)
                for line in (dataset / "meta" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            episodes_jsonl = [
                json.loads(line)
                for line in (dataset / "meta" / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(tasks[0]["__index_level_0__"], expected)
            self.assertEqual(episodes[0]["tasks"], [expected])
            self.assertEqual(tasks_jsonl[0]["task"], expected)
            self.assertEqual(episodes_jsonl[0]["tasks"], [expected])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
