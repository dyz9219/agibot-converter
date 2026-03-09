from __future__ import annotations

import unittest
from pathlib import Path
import shutil
import uuid

from data_converter.converters import lerobot_runner


class LerobotLayoutTests(unittest.TestCase):
    def test_flattens_single_agibotworld_task_directory(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"layout-{uuid.uuid4().hex[:8]}"
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=False)
        try:
            nested = root / "agibotworld" / "task_693000"
            (nested / "data").mkdir(parents=True)
            (nested / "meta").mkdir(parents=True)
            (nested / "videos").mkdir(parents=True)
            (nested / "meta" / "info.json").write_text('{"codebase_version":"v3.0"}', encoding="utf-8")
            (nested / "data" / "sample.parquet").write_text("ok", encoding="utf-8")
            (nested / "videos" / "sample.mp4").write_text("ok", encoding="utf-8")

            lerobot_runner._flatten_generated_dataset_layout(root)

            self.assertTrue((root / "meta" / "info.json").exists())
            self.assertTrue((root / "data" / "sample.parquet").exists())
            self.assertTrue((root / "videos" / "sample.mp4").exists())
            self.assertFalse((root / "agibotworld").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
