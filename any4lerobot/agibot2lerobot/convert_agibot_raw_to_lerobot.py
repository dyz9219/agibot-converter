#!/usr/bin/env python3
"""
Convert AGIBot Raw (Real Robot) datasets to LeRobot dataset format.

This script is designed for datasets with the following structure:
<episode_id>/
  data_info.json
  record/
    raw_joints.h5
  camera/
    head_color/color/00000000.jpg
    hand_left/color/00000000.jpg
    ...

Key changes from original:
- Reads `record/raw_joints.h5` instead of `aligned_joints.h5`
- Adapts to nested camera directory structure
- Uses frame index matching for image loading
"""

from __future__ import annotations

import ctypes
import gc
import json
import logging
import shutil
import signal
import sys
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import h5py
import numpy as np
from tqdm import tqdm
import tyro

from lerobot.common.constants import HF_LEROBOT_HOME
from lerobot.common.datasets.lerobot_dataset import LeRobotDataset

# ---------------------------------------------------------------------------- #
# Logging
# ---------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- #
# Global dataset reference for signal handling
# ---------------------------------------------------------------------------- #
_global_dataset: Optional[LeRobotDataset] = None


def _signal_handler(signum, frame):
    """Handle keyboard interrupt and clean up resources."""
    logger.warning(f"\nReceived signal {signum}, cleaning up...")
    if _global_dataset is not None:
        try:
            _global_dataset.stop_image_writer()
            logger.info("Image writer stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping image writer: {e}")
    sys.exit(1)


# ---------------------------------------------------------------------------- #
# Utility functions
# ---------------------------------------------------------------------------- #
def resize_image(image: np.ndarray, camera_type: str = "hand") -> np.ndarray:
    """Resize image keeping aspect ratios required by LeRobot using pure OpenCV."""
    target_h, target_w = (720, 1280) if camera_type == "head" else (480, 848)
    # target_h, target_w = (360, 640) if camera_type == "head" else (240, 424)
    if image.shape[0] == target_h and image.shape[1] == target_w:
        return image

    if image.ndim == 3:
        return cv2.resize(image, (target_w, target_h), interpolation=cv2.INTER_AREA)
    return cv2.resize(image, (target_w, target_h), interpolation=cv2.INTER_NEAREST)


def get_episode_dirs(data_dir: Path) -> List[Path]:
    """Collect episode directories from AGIBot Raw directory layout."""
    episode_dirs: List[Path] = []
    if not data_dir.exists():
        logger.warning(f"Data directory {data_dir} does not exist.")
        return []

    # Structure: root -> episode_id (digits) -> record/raw_joints.h5
    for ep_dir in data_dir.iterdir():
        if ep_dir.is_dir() and ep_dir.name.isdigit():
            # Check for required files
            h5_path = ep_dir / "record" / "raw_joints.h5"
            if h5_path.exists():
                episode_dirs.append(ep_dir)
            else:
                # Debug log if needed, or check if it's just a non-episode dir
                pass
                
    return sorted(episode_dirs, key=lambda x: int(x.name))


def get_camera_frames(ep_dir: Path) -> List[int]:
    """Get sorted camera frame indices."""
    # Use head_color as reference for frame indices
    cam_dir = ep_dir / "camera" / "head_color" / "color"
    if not cam_dir.exists():
        # Fallback to check if maybe it's under 'head_color' directly? 
        # But prompt said: head_color/color
        return []

    frame_indices = []
    for p in cam_dir.iterdir():
        if p.is_file() and p.stem.isdigit():
            # Expect filenames like 00000000.jpg
            try:
                frame_indices.append(int(p.stem))
            except ValueError:
                pass
    
    return sorted(frame_indices)

# 在此处新增了时间戳对齐函数---------------------------------------
def load_camera_timestamps(ts_file: Path) -> np.ndarray:
    """
    从d405_1_jpg.txt读取相机时间戳
    返回64位整型时间戳列表（移除了其中的小数点）
    """
    if not ts_file.exists():
        raise FileNotFoundError(ts_file)

    cam_ts = []
    with open(ts_file, "r") as f:
        for line in f:
            if not line.strip():
                continue
            parts = line.strip().split()
            # 第3列：1766392414013.895264
            ts_str = parts[2].replace(".", "")
            cam_ts.append(np.int64(ts_str))

    return np.asarray(cam_ts, dtype=np.int64)
# --------------------------------------------------------------


# 在此处新增了时间戳对齐函数以对齐照片和关节数据--------------------
def align_joints_to_camera(
    joint_ts: np.ndarray,
    joint_actions: np.ndarray,
    cam_ts: np.ndarray,
):
    """
    对于每张照片选择时间戳上最为接近的关节数据
    """
    joint_ts = joint_ts.astype(np.int64)
    cam_ts = cam_ts.astype(np.int64)

    # 在 joint_ts 中找 cam_ts 的插入位置
    idxs = np.searchsorted(joint_ts, cam_ts, side="left")
    idxs = np.clip(idxs, 0, len(joint_ts) - 1)

    prev = np.clip(idxs - 1, 0, len(joint_ts) - 1)

    # 比较前后哪个更近
    choose_prev = np.abs(cam_ts - joint_ts[prev]) <= np.abs(cam_ts - joint_ts[idxs])

    final_idxs = idxs.copy()
    final_idxs[choose_prev] = prev[choose_prev]

    aligned_actions = joint_actions[final_idxs]
    aligned_timestamps = joint_ts[final_idxs]

    return aligned_actions, aligned_timestamps, final_idxs
# --------------------------------------------------------------


def load_h5_data(h5_file: Path) -> Optional[Dict]:
    """
    Load joint data from raw_joints.h5 file.

    Assumes similar internal structure to aligned_joints.h5 for now:
    - action/joint/position
    - action/left_effector/position
    - action/right_effector/position
    """
    if not h5_file.exists():
        logger.warning(f"H5 file not found: {h5_file}")
        return None

    try:
        with h5py.File(h5_file, 'r') as f:
            # Validate required 'state' datasets based on known H5 structure
            required_paths = [
                'state/joint/position',
                'state/joint/timestamp',
                'state/left_effector/position',
                'state/right_effector/position',
            ]
            missing = []
            for p in required_paths:
                try:
                    _ = f[p]
                except KeyError:
                    missing.append(p)
            if missing:
                logger.error(f"H5 file missing required datasets: {missing}. Available top-level keys: {list(f.keys())}")
                return None

            # Read joint positions and timestamps (joint timeline is the reference)
            action_joints = f['state/joint/position'][:]
            ts_j = f['state/joint/timestamp'][:]

            # Read gripper positions and timestamps (timestamps may be missing)
            action_left_gripper = f['state/left_effector/position'][:]
            try:
                ts_l = f['state/left_effector/timestamp'][:]
            except KeyError:
                ts_l = None

            action_right_gripper = f['state/right_effector/position'][:]
            try:
                ts_r = f['state/right_effector/timestamp'][:]
            except KeyError:
                ts_r = None

            # Ensure gripper arrays are 2D
            def _ensure_2d(x):
                return x if x.ndim == 2 else x.reshape(-1, 1)

            action_left_gripper = _ensure_2d(action_left_gripper)
            action_right_gripper = _ensure_2d(action_right_gripper)

            # Align gripper streams to the joint timeline using nearest-neighbor on timestamps,
            # with ratio-based fallback when timestamps are missing.
            def _align_nn(ref_ts, tgt_ts, tgt_data):
                ref_ts = ref_ts.astype(np.int64)
                tgt_ts = tgt_ts.astype(np.int64)
                idxs = np.searchsorted(tgt_ts, ref_ts, side='left')
                idxs = np.clip(idxs, 0, len(tgt_ts) - 1)
                prev = np.clip(idxs - 1, 0, len(tgt_ts) - 1)
                choose_prev = np.abs(ref_ts - tgt_ts[prev]) <= np.abs(ref_ts - tgt_ts[idxs])
                mapped = tgt_data[idxs].copy()
                mapped[choose_prev] = tgt_data[prev][choose_prev]
                return mapped

            def _map_by_ratio(tgt_data, Nref):
                Ntgt = len(tgt_data)
                if Ntgt == 0:
                    # If gripper stream is empty, fill with zeros
                    return np.zeros((Nref, tgt_data.shape[1]), dtype=tgt_data.dtype)
                idxs = np.linspace(0, Ntgt - 1, Nref).round().astype(np.int64)
                return tgt_data[idxs]

            if ts_l is not None and len(ts_l) > 0:
                left_aligned = _align_nn(ts_j, ts_l, action_left_gripper)
            else:
                left_aligned = _map_by_ratio(action_left_gripper, len(ts_j))

            if ts_r is not None and len(ts_r) > 0:
                right_aligned = _align_nn(ts_j, ts_r, action_right_gripper)
            else:
                right_aligned = _map_by_ratio(action_right_gripper, len(ts_j))

            # Mirror gripper values: use 1 - effector_value
            """
            此处作了修改！由于真机采集的数据不是归一化的，直接用“1-原始数据”会出现负值导致夹爪数据异常无法抓取物体
            原始夹爪数据取值为0-120，据此修改代码将数据归一化，并将数据反相以匹配仿真时的状态关系
            原始代码为：
            action_left_gripper_mirrored = 1.0 - left_aligned
            action_right_gripper_mirrored = 1.0 - right_aligned
            """
            
            action_left_gripper_mirrored = (120.0 - left_aligned) / 120
            action_right_gripper_mirrored = (120.0 - right_aligned) /120

            # 将数据整合：左手7个关节、右手7个关节以及左、右手夹爪数据，请根据实际要求调整这四种数据的映射关系
            actions = np.concatenate([
                action_joints[:, :7],                # Left arm joints
                action_joints[:, 7:14],              # Right arm joints
                action_left_gripper_mirrored,        # Left gripper (mirrored)
                action_right_gripper_mirrored,       # Right gripper (mirrored)
            ], axis=1).astype(np.float32)

            timestamps = ts_j.astype(np.float64)
            num_frames = len(timestamps)

            logger.info(f"Loaded H5 data: {num_frames} frames, actions shape={actions.shape}")

            return {
                "actions": actions,
                "timestamps": timestamps,
                "num_frames": num_frames,
            }

    except Exception as e:
        logger.error(f"Failed to load H5 file {h5_file}: {e}")
        return None


def load_episode_info(ep_dir: Path, default_task: str) -> Dict:
    """Load episode info from data_info.json if exists."""
    info_file = ep_dir / "data_info.json"
    if info_file.exists():
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
            return {
                "task": info.get("english_task_name", default_task),
                "action_configs": info.get("label_info", {}).get("action_config", []),
            }
        except Exception as e:
            logger.warning(f"Failed to parse {info_file}: {e}")

    return {
        "task": default_task,
        "action_configs": [],
    }


def load_frame_data(ep_dir: Path, frame_idx: int, *, get_depth: bool = False) -> Dict:
    """Load RGB (and optionally depth) images for one frame."""
    result: Dict[str, np.ndarray] = {}
    
    # 8-digit zero padded filename
    fname = f"{frame_idx:08d}.jpg" 
    
    base_cam = ep_dir / "camera"
    
    # Structure from prompt:
    # head_color/color
    # hand_left/color
    # hand_right/color
    
    img_paths = {
        "hand_left_color": base_cam / "hand_left" / "color" / fname,
        "hand_right_color": base_cam / "hand_right" / "color" / fname,
        "head_color": base_cam / "head_color" / "color" / fname,
    }

    # Depth is not fully supported yet for .yuv/.txt as per prompt description
    # But if get_depth is requested, we can try to look for standard formats or log warning
    if get_depth:
        # Placeholder for depth paths if they follow similar structure
        pass

    for key, fp in img_paths.items():
        if not fp.exists():
            # Try png just in case
            fp_png = fp.with_suffix(".png")
            if fp_png.exists():
                fp = fp_png
            else:
                continue
                
        try:
            img = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
            if img is None:
                continue
            cam_type = "head" if "head" in key else "hand"
            result[key] = resize_image(img, cam_type)
        except Exception as e:
            logger.warning(f"Failed to load {fp}: {e}")
            continue

    return result


def force_gc():
    """Force garbage collection and memory trim."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


# ---------------------------------------------------------------------------- #
# Main episode processing
# ---------------------------------------------------------------------------- #
def process_episode(
    ep_dir: Path,
    ds: LeRobotDataset,
    *,
    frame_stride: int,
    default_task: str,
    get_depth: bool = False,
    overwrite: bool = False,
) -> bool:
    """
    Process a single episode and add frames to the dataset.
    """
    done_flag = ep_dir / ".ConverteD"
    if done_flag.exists() and not overwrite:
        logger.info(f"Skip already converted {ep_dir}")
        return False

    h5_file = ep_dir / "record" / "raw_joints.h5"
    h5_data = load_h5_data(h5_file)  # 读取.h5文件（关节数据）
    if h5_data is None:
        return False

    ep_info = load_episode_info(ep_dir, default_task)  # 读取打标文件
    frames = get_camera_frames(ep_dir) # List[int]  获取所有图片文件的目录

    if not frames:
        del h5_data
        return False

    """
    --------------------------------------------------------------------------------------
    在此处修改了裁剪逻辑，按照相机的帧数，保留与相机时间戳最为对应的关节数据，其余的关节数据不予保留
    原始代码为：
    max_frames = min(len(frames), h5_data["num_frames"])
    """
    # 读取相机时间戳（以左手相机时间戳作为基准）
    cam_ts_file = ep_dir / "camera" / "hand_left" / "d405_1_jpg.txt"
    cam_ts = load_camera_timestamps(cam_ts_file)

    # 用相机时间戳对齐关节数据
    actions_aligned, ts_aligned, joint_indices = align_joints_to_camera(
        h5_data["timestamps"],
        h5_data["actions"],
        cam_ts,
    )

    # 用“对齐后”的数据替换原始数据
    h5_data["actions"] = actions_aligned
    h5_data["timestamps"] = ts_aligned
    h5_data["num_frames"] = len(ts_aligned)

    # frames 也要裁剪成同样长度（通常是一一对应）
    frames = frames[:len(ts_aligned)]
    max_frames = len(ts_aligned)

    # -------------------------------------------------------------------------------------

    if max_frames == 0:
        del h5_data
        return False

    task_prompt = ep_info["task"]
    action_configs = ep_info["action_configs"]

    processed = False
    
    # Need to handle frame ranges carefully since frames is a list of indices, potentially non-contiguous?
    # Usually they are contiguous. We will iterate through valid indices.
    
    if action_configs:
        for cfg in action_configs:
            start = cfg.get("start_frame", 0)
            end = cfg.get("end_frame", max_frames)
            action_text = cfg.get("english_action_text", cfg.get("action_text", ""))

            start = max(0, min(start, max_frames - 1))
            end = min(end, max_frames)

            if start >= end - frame_stride:
                continue

            _process_frame_range(
                ep_dir, ds, h5_data, frames,
                start, end, frame_stride,
                task_prompt, action_text, get_depth
            )
            processed = True
    else:
        _process_frame_range(
            ep_dir, ds, h5_data, frames,
            0, max_frames, frame_stride,
            task_prompt, "", get_depth
        )
        processed = True

    if processed:
        ds.save_episode()

        try:
            done_flag.touch(exist_ok=True)
        except Exception as e:
            logger.warning(f"Failed to write done flag for {ep_dir}: {e}")

        logger.info(f"Saved episode from {ep_dir}")

    # Local cleanup
    del h5_data
    force_gc()

    return processed


def _process_frame_range(
    ep_dir: Path,
    ds: LeRobotDataset,
    h5_data: Dict,
    frames: List[int],
    start: int,
    end: int,
    frame_stride: int,
    task_prompt: str,
    action_text: str,
    get_depth: bool,
) -> None:
    """
    Process a range of frames within an episode.
    """
    sample_idxs = range(start, end - frame_stride)

    for idx in sample_idxs:
        # idx is the index in the sequence (0..N).
        # We assume frames[idx] gives the file index for image loading.
        # And idx gives the index in H5.
        
        # Use current frame's action as observation state
        cur_state = h5_data["actions"][idx]

        # Use future frame's action as the action to predict
        future_idx = idx + frame_stride
        if future_idx >= h5_data["num_frames"]:
            break
        action = h5_data["actions"][future_idx]

        if idx >= len(frames):
            break
        
        frame_idx_num = frames[idx]
        frame_data = load_frame_data(ep_dir, frame_idx_num, get_depth=get_depth)

        if not all(k in frame_data for k in ["hand_left_color", "hand_right_color", "head_color"]):
            del frame_data
            continue

        task_str = f"{task_prompt}: {action_text}" if action_text else task_prompt
        sample = {
            "task": task_str,
            "observation.state": cur_state,
            "actions": action,
            "observation.images.hand_left_color": frame_data["hand_left_color"],
            "observation.images.hand_right_color": frame_data["hand_right_color"],
            "observation.images.head_color": frame_data["head_color"],
        }

        if get_depth:
            for dk in ["hand_left_depth", "hand_right_depth", "head_depth"]:
                if dk in frame_data:
                    sample[f"observation.images.{dk}"] = frame_data[dk]

        ds.add_frame(sample)

        del frame_data
        del sample


def create_dataset(
    repo_id: str,
    out_path: Path,
    *,
    get_depth: bool = False,
    image_writer_threads: int = 0,
    image_writer_processes: int = 0,
    resume: bool = True,
) -> LeRobotDataset:
    """Create or resume a LeRobot dataset with proper features definition."""

    # State and action have the same dimension: 16
    # [left_joints(7), left_gripper(1), right_joints(7), right_gripper(1)]
    state_action_dim = 16

    features = {
        "observation.images.hand_left_color": {
            "dtype": "image",
            "shape": (480, 848, 3),
            "names": ["height", "width", "channels"],
        },
        "observation.images.hand_right_color": {
            "dtype": "image",
            "shape": (480, 848, 3),
            "names": ["height", "width", "channels"],
        },
        "observation.images.head_color": {
            "dtype": "image",
            "shape": (720, 1280, 3),
            "names": ["height", "width", "channels"],
        },
        "observation.state": {
            "dtype": "float32",
            "shape": (state_action_dim,),
            "names": ["joint_positions"],
        },
        "actions": {
            "dtype": "float32",
            "shape": (state_action_dim,),
            "names": ["joint_actions"],
        },
    }

    if get_depth:
        features.update({
            "observation.images.hand_left_depth": {
                "dtype": "image",
                "shape": (480, 848, 3),
                "names": ["height", "width", "channels"],
            },
            "observation.images.hand_right_depth": {
                "dtype": "image",
                "shape": (480, 848, 3),
                "names": ["height", "width", "channels"],
            },
            "observation.images.head_depth": {
                "dtype": "image",
                "shape": (720, 1280, 3),
                "names": ["height", "width", "channels"],
            },
        })

    info_json = out_path / "meta" / "info.json"
    tasks_jsonl = out_path / "meta" / "tasks.jsonl"

    if resume and info_json.exists() and tasks_jsonl.exists():
        dataset = LeRobotDataset(
            repo_id=repo_id,
            root=out_path,
            download_videos=False,
        )
        logger.info(f"Resuming existing dataset at {out_path}")
    else:
        if resume and out_path.exists():
            logger.warning(f"Resume requested but dataset at {out_path} is incomplete/corrupted. Re-creating...")
            shutil.rmtree(out_path)

        dataset = LeRobotDataset.create(
            repo_id=repo_id,
            robot_type="agibot",
            fps=30,
            features=features,
            use_videos=True,
        )
        logger.info(f"Created new dataset at {out_path}")

    if image_writer_threads > 0:
        dataset.start_image_writer(
            num_processes=image_writer_processes,
            num_threads=image_writer_threads
        )
        logger.info(f"Async image writer started with threads={image_writer_threads}")

    # Disable batch encoding to reduce memory usage
    dataset.batch_encoding_size = 1
    logger.info("Batch encoding disabled (batch_encoding_size=1)")

    return dataset


def reinitialize_dataset(
    repo_id: str,
    out_path: Path,
    *,
    image_writer_threads: int = 0,
    image_writer_processes: int = 0,
) -> LeRobotDataset:
    """Reinitialize dataset to clear memory (resume mode)."""
    dataset = LeRobotDataset(
        repo_id=repo_id,
        root=out_path,
        download_videos=False,
    )

    dataset.batch_encoding_size = 1

    if image_writer_threads > 0:
        dataset.start_image_writer(
            num_processes=image_writer_processes,
            num_threads=image_writer_threads
        )

    return dataset


# ---------------------------------------------------------------------------- #
# CLI & Entry
# ---------------------------------------------------------------------------- #
def main(
    data_dirs: List[Path],
    repo_id: str,
    *,
    task_name: str = "make a sandwich",
    frame_stride: int = 1,
    get_depth: bool = False,
    push_to_hub: bool = False,
    resume: bool = True,
    overwrite: bool = False,
    image_writer_threads: int = 0,
    image_writer_processes: int = 0,
    reset_interval: int = 40,
):
    """
    Convert multiple AGIBot Raw datasets to LeRobot format.

    Args:
        data_dirs: List of directories containing AGIBot data
        repo_id: Repository ID for the output dataset
        task_name: Default task name if not found in data_info.json
        frame_stride: Frame stride for action prediction (default: 1)
        get_depth: Whether to include depth images
        push_to_hub: Whether to push to Hugging Face Hub
        resume: Whether to resume from existing dataset
        image_writer_threads: Number of image writer threads (0 to disable async)
        image_writer_processes: Number of image writer processes
        reset_interval: Reinitialize dataset every N episodes to clear memory
        overwrite: Whether to overwrite already converted episodes
    """
    global _global_dataset

    # If overwrite is requested, we should probably warn or ensure consistency.
    # But usually overwrite implies we want to re-do work.
    
    # Register signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    out_path = HF_LEROBOT_HOME / repo_id
    if out_path.exists() and not resume:
        shutil.rmtree(out_path)
        logger.info(f"Removed existing dataset at {out_path} for fresh run")

    try:
        # Initialize dataset
        dataset = create_dataset(
            repo_id,
            out_path,
            get_depth=get_depth,
            image_writer_threads=image_writer_threads,
            image_writer_processes=image_writer_processes,
            resume=resume,
        )
        _global_dataset = dataset

        processed_count = 0

        # Main loop over all data directories
        for d in data_dirs:
            logger.info(f"Traversing {d}")
            ep_dirs = get_episode_dirs(d)
            logger.info(f"Found {len(ep_dirs)} episodes in {d}")

            for ep in tqdm(ep_dirs, desc=f"Episodes in {d}"):
                try:
                    success = process_episode(
                        ep,
                        dataset,
                        frame_stride=frame_stride,
                        default_task=task_name,
                        get_depth=get_depth,
                        overwrite=overwrite,
                    )

                    if success:
                        # Ensure current episode is written
                        dataset._wait_image_writer()
                        processed_count += 1

                        # Periodically reinitialize dataset to clear memory
                        if reset_interval > 0 and processed_count % reset_interval == 0:
                            logger.info(f"♻️  Re-initializing dataset to clear memory (Count: {processed_count})...")

                            # Stop image writer
                            if image_writer_threads > 0:
                                dataset.stop_image_writer()

                            # Destroy Python object
                            del dataset
                            _global_dataset = None

                            # Force memory reclamation
                            force_gc()

                            # Reload dataset in resume mode
                            dataset = reinitialize_dataset(
                                repo_id,
                                out_path,
                                image_writer_threads=image_writer_threads,
                                image_writer_processes=image_writer_processes,
                            )
                            _global_dataset = dataset

                            logger.info("✅ Dataset re-initialized. Memory flushed.")

                except Exception as e:
                    logger.error(f"Failed to process episode {ep}: {e}")
                    force_gc()
                    continue

        # Finalization
        logger.info("Waiting for final image writes to complete...")
        if image_writer_threads > 0:
            dataset.stop_image_writer()

        if push_to_hub:
            dataset.push_to_hub(
                tags=["agibot", "16dof", "manipulation", "raw"],
                private=False,
                push_videos=True,
                license="apache-2.0",
            )
            logger.info("Dataset pushed to Hugging Face Hub")

        logger.info(f"Conversion completed! Total episodes processed: {processed_count}")

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        raise
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        # Cleanup resources
        if _global_dataset is not None:
            try:
                if image_writer_threads > 0:
                    _global_dataset.stop_image_writer()
                logger.info("Image writer stopped")
            except Exception as e:
                logger.error(f"Error stopping image writer during cleanup: {e}")
        force_gc()
        logger.info("Cleanup completed")


if __name__ == "__main__":
    tyro.cli(main)