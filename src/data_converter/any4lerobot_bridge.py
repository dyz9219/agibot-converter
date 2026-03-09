from __future__ import annotations

import argparse
import copy
import importlib
import shutil
import sys
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from .any4lerobot_locator import find_any4lerobot_root


@dataclass(slots=True)
class Any4RunResult:
    returncode: int
    error: str = ""
    stdout: str = ""
    stderr: str = ""


def run_any4lerobot_cli_result(argv: list[str]) -> Any4RunResult:
    out_buf = StringIO()
    err_buf = StringIO()

    def _combine(detail: str) -> str:
        stdout = out_buf.getvalue().strip()
        stderr = err_buf.getvalue().strip()
        parts = [detail]
        if stdout:
            parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        return "\n\n".join(parts)

    root = find_any4lerobot_root()
    if root is not None:
        sys.path.insert(0, str(root))
        # Also add agibot2lerobot directory for direct imports like agibot_utils
        agibot2lerobot_dir = root / "agibot2lerobot"
        if agibot2lerobot_dir.exists():
            sys.path.insert(0, str(agibot2lerobot_dir))

    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            agibot_h5 = importlib.import_module("agibot2lerobot.agibot_h5")
    except Exception as exc:
        detail = _combine(f"加载 any4lerobot 失败: {exc}\n{traceback.format_exc()}")
        print(detail, file=sys.stderr)
        return Any4RunResult(
            returncode=2,
            error=detail,
            stdout=out_buf.getvalue(),
            stderr=err_buf.getvalue(),
        )

    parser = argparse.ArgumentParser(prog="any4lerobot", add_help=True)
    parser.add_argument("--src-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--eef-type", type=str, choices=["gripper", "dexhand", "tactile"], default="gripper")
    parser.add_argument("--task-ids", type=str, nargs="+", default=[])
    parser.add_argument("--cpus-per-task", type=int, default=3)
    parser.add_argument("--save-depth", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            _install_any4_runtime_patches(agibot_h5)
            with _patch_any4_image_config(agibot_h5, args.src_path, args.eef_type, args.save_depth):
                agibot_h5.main(**vars(args))
        return Any4RunResult(returncode=0, stdout=out_buf.getvalue(), stderr=err_buf.getvalue())
    except Exception as exc:
        detail = _combine(f"any4lerobot 执行失败: {exc}\n{traceback.format_exc()}")
        print(detail, file=sys.stderr)
        return Any4RunResult(
            returncode=1,
            error=detail,
            stdout=out_buf.getvalue(),
            stderr=err_buf.getvalue(),
        )


def run_any4lerobot_cli(argv: list[str]) -> int:
    return run_any4lerobot_cli_result(argv).returncode


def _install_any4_runtime_patches(agibot_h5_module) -> None:
    dataset_cls = getattr(agibot_h5_module, "AgiBotDataset", None)
    if dataset_cls is None:
        return
    if getattr(dataset_cls, "_data_converter_temp_patch", False):
        return

    def _encode_temporary_episode_video(self, video_key: str, episode_index: int) -> Path:
        temp_base = Path.cwd() / ".tmp-any4-video"
        temp_base.mkdir(parents=True, exist_ok=True)
        temp_dir = temp_base / f"{video_key}_{episode_index:03d}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=False)
        temp_path = temp_dir / f"{video_key}_{episode_index:03d}.mp4"
        shutil.copy2(self.current_videos[video_key], temp_path)
        return temp_path

    dataset_cls._encode_temporary_episode_video = _encode_temporary_episode_video
    dataset_cls._data_converter_temp_patch = True


@contextmanager
def _patch_any4_image_config(agibot_h5_module, src_path: Path, eef_type: str, save_depth: bool):
    task_type_map = getattr(agibot_h5_module, "AgiBotWorld_TASK_TYPE", None)
    if not isinstance(task_type_map, dict) or eef_type not in task_type_map:
        yield
        return

    task_entry = task_type_map[eef_type]
    task_config = task_entry.get("task_config")
    if not isinstance(task_config, dict):
        yield
        return

    base_images = task_config.get("images")
    if not isinstance(base_images, dict):
        yield
        return

    detected = _detect_any4_video_files(src_path)
    patched_images = _build_dynamic_image_config(base_images, detected, save_depth=save_depth)
    original_images = copy.deepcopy(base_images)
    task_config["images"] = patched_images
    try:
        yield
    finally:
        task_config["images"] = original_images


def _detect_any4_video_files(src_path: Path) -> dict[str, Path]:
    observations_dir = src_path / "observations"
    if not observations_dir.is_dir():
        return {}

    key_sets: list[set[str]] = []
    representative_paths: dict[str, Path] = {}
    for videos_dir in observations_dir.glob("*/*/videos"):
        if not videos_dir.is_dir():
            continue
        episode_map: dict[str, Path] = {}
        for mp4 in sorted(videos_dir.glob("*.mp4")):
            key = mp4.stem.removesuffix("_color")
            episode_map[key] = mp4
            representative_paths.setdefault(key, mp4)
        if episode_map:
            key_sets.append(set(episode_map))

    if not key_sets:
        return {}

    common_keys = set.intersection(*key_sets)
    return {key: representative_paths[key] for key in sorted(common_keys) if key in representative_paths}


def _build_dynamic_image_config(base_images: dict, detected_videos: dict[str, Path], save_depth: bool) -> dict:
    if not detected_videos:
        images = copy.deepcopy(base_images)
        return images

    images: dict[str, dict] = {}
    for key, spec in base_images.items():
        dtype = spec.get("dtype")
        if dtype == "image":
            images[key] = copy.deepcopy(spec)
            continue
        if key in detected_videos:
            images[key] = copy.deepcopy(spec)

    for key, path in detected_videos.items():
        if key in images:
            continue
        images[key] = _make_generic_video_spec(path)
    return images


def _make_generic_video_spec(path: Path) -> dict:
    height, width = _probe_video_shape(path)
    return {
        "dtype": "video",
        "shape": (height, width, 3),
        "names": ["height", "width", "rgb"],
    }


def _probe_video_shape(path: Path) -> tuple[int, int]:
    try:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(str(path))
        try:
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        finally:
            cap.release()
        if width > 0 and height > 0:
            return height, width
    except Exception:
        pass
    return 480, 640
