from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import h5py
import numpy as np

from data_converter.adapters.raw_to_any4 import _build_proprio_stats, prepare_any4_source


class RawToAny4AdapterTests(unittest.TestCase):
    def test_uses_parent_annotation_result_action_text_for_task_scene(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"raw-adapter-{uuid.uuid4().hex[:8]}"
        source = root / "episode_parent"
        raw_dir = source / "5"
        raw_dir.mkdir(parents=True, exist_ok=False)
        try:
            (source / "annotation_result.json").write_text(
                json.dumps(
                    [{"start_frame": 0, "end_frame": 10, "action_text": "把瓶子放进筐子里"}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (raw_dir / "state.json").write_text("{}", encoding="utf-8")
            (raw_dir / "head.mp4").write_bytes(b"fake-mp4")
            with h5py.File(raw_dir / "aligned_joints.h5", "w") as h5f:
                h5f.create_dataset("timestamp", data=np.array([0.0], dtype=np.float32))

            result = prepare_any4_source(
                source, source_name="platform_sample", work_root=root / "work"
            )
            task_info_path = next((result.prepared_root / "task_info").glob("*.json"))
            task_info = json.loads(task_info_path.read_text(encoding="utf-8"))

            self.assertEqual(task_info[0]["init_scene_text"], "把瓶子放进筐子里")
            raw_task_info = task_info_path.read_bytes()
            self.assertNotIn("把瓶子放进筐子里".encode("utf-8"), raw_task_info)
            self.assertIn(b"\\u628a\\u74f6\\u5b50", raw_task_info)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_preserves_joint_data_from_16d_raw_arrays(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"raw-adapter-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            src_h5 = root / "aligned_joints.h5"
            dst_h5 = root / "proprio_stats.h5"
            state_joint = np.arange(32, dtype=np.float32).reshape(2, 16)
            action_joint = np.arange(32, dtype=np.float32).reshape(2, 16) + 100.0

            with h5py.File(src_h5, "w") as h5f:
                h5f.create_dataset("timestamp", data=np.array([0.0, 1.0], dtype=np.float32))
                h5f.create_dataset("state/joint/position", data=state_joint)
                h5f.create_dataset("action/joint/position", data=action_joint)

            warnings: list[str] = []
            _build_proprio_stats(src_h5, dst_h5, warnings)

            with h5py.File(dst_h5, "r") as h5f:
                np.testing.assert_allclose(h5f["state/joint/position"][:], state_joint)
                np.testing.assert_allclose(h5f["action/joint/position"][:], action_joint)
                np.testing.assert_allclose(h5f["state/effector/position"][:], state_joint[:, 14:16])
                np.testing.assert_allclose(
                    h5f["action/effector/position"][:], action_joint[:, 14:16]
                )

            self.assertFalse(
                any(
                    "state/joint/position" in warning and "填充零值" in warning
                    for warning in warnings
                ),
                warnings,
            )
            self.assertFalse(
                any(
                    "action/joint/position" in warning and "填充零值" in warning
                    for warning in warnings
                ),
                warnings,
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
