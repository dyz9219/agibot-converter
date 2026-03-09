from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np


@dataclass(slots=True)
class AgibotDataset:
    root_dir: Path
    fps: float
    timestamps: np.ndarray
    joint_names: list[str]
    joint_position: np.ndarray
    joint_velocity: np.ndarray
    joint_effort: np.ndarray
    camera_videos: dict[str, Path]


def load_agibot_dataset(source_dir: Path, fps_fallback: float = 30.0) -> AgibotDataset:
    root_dir = _resolve_dataset_root(source_dir)
    h5_path = root_dir / "aligned_joints.h5"
    state_path = root_dir / "state.json"
    if not h5_path.exists():
        raise FileNotFoundError(f"未找到 aligned_joints.h5: {root_dir}")
    if not state_path.exists():
        raise FileNotFoundError(f"未找到 state.json: {root_dir}")

    with h5py.File(h5_path, "r") as h5f:
        joint_position = _required_array(h5f, "state/joint/position", np.float64)
        if joint_position.ndim != 2:
            raise ValueError(f"state/joint/position 维度异常: {joint_position.shape}")
        frame_count, joint_count = joint_position.shape
        joint_velocity = _optional_array(h5f, "state/joint/velocity", (frame_count, joint_count), np.float64)
        joint_effort = _optional_array(h5f, "state/joint/effort", (frame_count, joint_count), np.float64)
        timestamps = _optional_1d(h5f, "timestamp", frame_count, fps_fallback)

    state_doc = json.loads(state_path.read_text(encoding="utf-8"))
    fps = float(state_doc.get("fps") or fps_fallback)
    joint_names = _resolve_joint_names(state_doc, joint_count)
    camera_videos = _resolve_camera_videos(root_dir)

    return AgibotDataset(
        root_dir=root_dir,
        fps=fps,
        timestamps=timestamps,
        joint_names=joint_names,
        joint_position=joint_position,
        joint_velocity=joint_velocity,
        joint_effort=joint_effort,
        camera_videos=camera_videos,
    )


def _resolve_dataset_root(source_dir: Path) -> Path:
    if (source_dir / "aligned_joints.h5").exists() and (source_dir / "state.json").exists():
        return source_dir

    direct = [p for p in sorted(source_dir.iterdir()) if p.is_dir()]
    for child in direct:
        if (child / "aligned_joints.h5").exists() and (child / "state.json").exists():
            return child

    for h5_path in source_dir.rglob("aligned_joints.h5"):
        parent = h5_path.parent
        if (parent / "state.json").exists():
            return parent

    raise FileNotFoundError(f"未识别到 Agibot 数据目录: {source_dir}")


def _required_array(h5f: h5py.File, key: str, dtype: type[np.float64]) -> np.ndarray:
    if key not in h5f:
        raise KeyError(f"H5 缺少关键字段: {key}")
    return np.asarray(h5f[key], dtype=dtype)


def _optional_array(
    h5f: h5py.File,
    key: str,
    shape: tuple[int, int],
    dtype: type[np.float64],
) -> np.ndarray:
    if key not in h5f:
        return np.zeros(shape, dtype=dtype)
    arr = np.asarray(h5f[key], dtype=dtype)
    if arr.shape == shape:
        return arr
    if arr.size == 0:
        return np.zeros(shape, dtype=dtype)
    if arr.ndim == 2:
        rows = min(shape[0], arr.shape[0])
        cols = min(shape[1], arr.shape[1])
        out = np.zeros(shape, dtype=dtype)
        out[:rows, :cols] = arr[:rows, :cols]
        return out
    raise ValueError(f"H5 字段形状不兼容: {key} -> {arr.shape}, 期望 {shape}")


def _optional_1d(h5f: h5py.File, key: str, frame_count: int, fps_fallback: float) -> np.ndarray:
    if key not in h5f:
        return np.arange(frame_count, dtype=np.float64) / max(fps_fallback, 1.0)
    arr = np.asarray(h5f[key], dtype=np.float64).reshape(-1)
    if arr.size == frame_count:
        return arr
    if arr.size == 0:
        return np.arange(frame_count, dtype=np.float64) / max(fps_fallback, 1.0)
    if arr.size > frame_count:
        return arr[:frame_count]
    pad = np.arange(arr.size, frame_count, dtype=np.float64) / max(fps_fallback, 1.0)
    return np.concatenate([arr, pad], axis=0)


def _resolve_joint_names(state_doc: dict, joint_count: int) -> list[str]:
    names = []
    robot = state_doc.get("robot") if isinstance(state_doc, dict) else None
    joints = robot.get("joints") if isinstance(robot, dict) else None
    if isinstance(joints, dict):
        names = joints.get("joint_name") or []
    if not names and isinstance(state_doc.get("frames"), list) and state_doc["frames"]:
        frame0 = state_doc["frames"][0]
        robot0 = frame0.get("robot") if isinstance(frame0, dict) else None
        joints0 = robot0.get("joints") if isinstance(robot0, dict) else None
        if isinstance(joints0, dict):
            names = joints0.get("joint_name") or []
    normalized = [str(n) for n in names]
    if len(normalized) == joint_count:
        return normalized
    if len(normalized) > joint_count:
        return normalized[:joint_count]
    for idx in range(len(normalized), joint_count):
        normalized.append(f"joint_{idx}")
    return normalized


def _resolve_camera_videos(root_dir: Path) -> dict[str, Path]:
    cameras: dict[str, Path] = {}
    for name in ("head", "hand_left", "hand_right", "whole_body"):
        p = root_dir / f"{name}.mp4"
        if p.exists():
            cameras[name] = p
    return cameras
