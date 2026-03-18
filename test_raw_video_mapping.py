from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import h5py
import numpy as np

from data_converter.adapters import raw_to_any4
from data_converter import any4lerobot_bridge


class RawVideoMappingTests(unittest.TestCase):
    def test_preserves_16d_joint_schema_and_effector_slice(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"raw-adapter-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            src_h5 = root / "aligned_joints.h5"
            dst_h5 = root / "proprio_stats.h5"
            state_joint = np.arange(32, dtype=np.float32).reshape(2, 16)
            action_joint = np.arange(32, dtype=np.float32).reshape(2, 16) + 100.0
            state_effector_bogus = np.full((2, 2), -1.0, dtype=np.float32)
            action_effector_bogus = np.full((2, 2), -2.0, dtype=np.float32)

            with h5py.File(src_h5, "w") as h5f:
                h5f.create_dataset("timestamp", data=np.array([0.0, 1.0], dtype=np.float32))
                h5f.create_dataset("state/joint/position", data=state_joint)
                h5f.create_dataset("action/joint/position", data=action_joint)
                h5f.create_dataset("state/effector/position", data=state_effector_bogus)
                h5f.create_dataset("action/effector/position", data=action_effector_bogus)

            warnings: list[str] = []
            raw_to_any4._build_proprio_stats(src_h5, dst_h5, warnings)

            with h5py.File(dst_h5, "r") as h5f:
                np.testing.assert_allclose(h5f["state/joint/position"][:], state_joint)
                np.testing.assert_allclose(h5f["action/joint/position"][:], action_joint)
                np.testing.assert_allclose(h5f["state/effector/position"][:], state_joint[:, 14:16])
                np.testing.assert_allclose(h5f["action/effector/position"][:], action_joint[:, 14:16])
                self.assertFalse(np.array_equal(h5f["state/effector/position"][:], state_effector_bogus))
                self.assertFalse(np.array_equal(h5f["action/effector/position"][:], action_effector_bogus))

            self.assertFalse(
                any("state/joint/position" in warning and "填充零值" in warning for warning in warnings),
                warnings,
            )
            self.assertFalse(
                any("action/joint/position" in warning and "填充零值" in warning for warning in warnings),
                warnings,
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_videos_copies_only_present_videos(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"video-map-{uuid.uuid4().hex[:8]}"
        raw_dir = root / "raw"
        videos_dir = root / "out"
        try:
            raw_dir.mkdir(parents=True, exist_ok=False)
            for name in ["head.mp4", "hand_left.mp4", "hand_right.mp4", "whole_body.mp4", "top_camera.mp4"]:
                (raw_dir / name).write_bytes(b"fake")

            warnings: list[str] = []
            raw_to_any4._build_videos(raw_dir, videos_dir, warnings)

            actual = sorted(p.name for p in videos_dir.glob("*.mp4"))
            self.assertEqual(
                actual,
                [
                    "hand_left_color.mp4",
                    "hand_right_color.mp4",
                    "head_color.mp4",
                    "top_camera_color.mp4",
                    "whole_body_color.mp4",
                ],
            )
            self.assertEqual(warnings, [])
            self.assertFalse((videos_dir / "head_center_fisheye_color.mp4").exists())
            self.assertFalse((videos_dir / "back_right_fisheye_color.mp4").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_videos_keeps_distinct_variant_names(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"video-variant-{uuid.uuid4().hex[:8]}"
        raw_dir = root / "raw"
        videos_dir = root / "out"
        try:
            raw_dir.mkdir(parents=True, exist_ok=False)
            for name in [
                "head.mp4",
                "hand_left.mp4",
                "hand_left - ИББО.mp4",
                "hand_right.mp4",
                "hand_right - ИББО.mp4",
                "whole_body.mp4",
            ]:
                (raw_dir / name).write_bytes(b"fake")

            warnings: list[str] = []
            raw_to_any4._build_videos(raw_dir, videos_dir, warnings)

            actual = sorted(p.name for p in videos_dir.glob("*.mp4"))
            self.assertEqual(len(actual), 6)
            self.assertIn("hand_left_color.mp4", actual)
            self.assertIn("hand_right_color.mp4", actual)
            self.assertIn("head_color.mp4", actual)
            self.assertIn("whole_body_color.mp4", actual)
            self.assertEqual(len([name for name in actual if name.startswith("hand_left_")]), 2)
            self.assertEqual(len([name for name in actual if name.startswith("hand_right_")]), 2)
            self.assertEqual(warnings, [])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_videos_sanitizes_non_ascii_names_to_ascii_safe_keys(self) -> None:
        root = Path.cwd() / ".tmp-tests" / f"video-nonascii-{uuid.uuid4().hex[:8]}"
        raw_dir = root / "raw"
        videos_dir = root / "out"
        try:
            raw_dir.mkdir(parents=True, exist_ok=False)
            for name in ["hand_left.mp4", "hand_left - ИББО.mp4", "纯中文.mp4"]:
                (raw_dir / name).write_bytes(b"fake")

            warnings: list[str] = []
            raw_to_any4._build_videos(raw_dir, videos_dir, warnings)

            actual = sorted(p.stem for p in videos_dir.glob("*.mp4"))
            self.assertIn("hand_left_color", actual)
            self.assertEqual(len(actual), 3)
            for stem in actual:
                self.assertRegex(stem, r"^[A-Za-z0-9_]+$")
            self.assertTrue(any(name.startswith("hand_left_") and name != "hand_left_color" for name in actual))
            self.assertTrue(any(name.startswith("camera_") for name in actual))
            self.assertEqual(warnings, [])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_dynamic_image_config_keeps_detected_keys(self) -> None:
        base_images = {
            "head": {"dtype": "video", "shape": (480, 640, 3), "names": ["height", "width", "rgb"]},
            "hand_left": {"dtype": "video", "shape": (480, 640, 3), "names": ["height", "width", "rgb"]},
            "hand_right": {"dtype": "video", "shape": (480, 640, 3), "names": ["height", "width", "rgb"]},
            "head_depth": {"dtype": "image", "shape": (480, 640, 1), "names": ["height", "width", "channel"]},
        }
        detected = {
            "head": Path("head_color.mp4"),
            "whole_body": Path("whole_body_color.mp4"),
            "top_camera": Path("top_camera_color.mp4"),
        }

        images = any4lerobot_bridge._build_dynamic_image_config(base_images, detected, save_depth=False)

        self.assertEqual(set(images.keys()), {"head", "head_depth", "whole_body", "top_camera"})
        self.assertEqual(images["whole_body"]["dtype"], "video")
        self.assertEqual(images["top_camera"]["dtype"], "video")

    def test_install_any4_runtime_patches_moves_temp_video_outside_dataset_root(self) -> None:
        class FakeDataset:
            def __init__(self) -> None:
                self.root = Path.cwd() / "fake-root"
                self.current_videos = {"head": Path(__file__)}

        module = SimpleNamespace(AgiBotDataset=FakeDataset)

        any4lerobot_bridge._install_any4_runtime_patches(module)
        dataset = FakeDataset()
        temp_path = dataset._encode_temporary_episode_video("head", 0)

        try:
            self.assertTrue(temp_path.exists())
            self.assertNotIn(str(dataset.root), str(temp_path))
            self.assertIn(".tmp-any4-video".lower(), str(temp_path).lower())
        finally:
            shutil.rmtree(temp_path.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
