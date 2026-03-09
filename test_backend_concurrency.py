from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from data_converter.backend import ConversionBackend, build_options
from data_converter.models import SourceItem, TaskPlan, TaskStatus


class BackendConcurrencyTests(unittest.TestCase):
    def test_lerobot_uses_frontend_concurrency_for_outer_workers(self) -> None:
        opts = build_options(
            input_path="in",
            output_path="out",
            target="lerobot",
            version="v3.0",
            fps="30",
            bag_type="MCAP",
            concurrency="7",
        )
        plans = [
            TaskPlan(
                task_id=f"task_{i}",
                source=SourceItem(name=f"s{i}", source_path=Path("in"), is_zip=False),
                output_dir=Path(f"out_{i}"),
                status=TaskStatus.PENDING,
            )
            for i in range(3)
        ]
        seen_workers: list[int | None] = []
        real_tpe = __import__("concurrent.futures").futures.ThreadPoolExecutor

        class SpyExecutor(real_tpe):
            def __init__(self, max_workers=None, *args, **kwargs):
                seen_workers.append(max_workers)
                super().__init__(max_workers=max_workers, *args, **kwargs)

        with (
            patch("data_converter.backend.ThreadPoolExecutor", SpyExecutor),
            patch.object(ConversionBackend, "_run_task", return_value=None),
        ):
            summary = ConversionBackend().run(opts, plans)

        self.assertEqual(seen_workers, [7])
        self.assertEqual(summary.success, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
