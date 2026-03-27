import json
from pathlib import Path

import h5py
import numpy as np
from PIL import Image


def get_task_info(task_json_path: str) -> dict:
    with open(task_json_path, "r") as f:
        task_info: list = json.load(f)
    task_info.sort(key=lambda episode: episode["episode_id"])
    return task_info


def load_depths(root_dir: str, camera_name: str):
    cam_path = Path(root_dir)
    all_imgs = sorted(list(cam_path.glob(f"{camera_name}*")))
    return [np.array(Image.open(f)).astype(np.float32)[:, :, None] / 1000 for f in all_imgs]


def load_local_dataset(
    episode_id: int, src_path: str, task_id: int, save_depth: bool, AgiBotWorld_CONFIG: dict
) -> tuple[int, list[dict[str, np.ndarray]], dict[str, Path]]:
    """Load local dataset and return a dict with observations and actions"""
    episode_id, columns, videos = load_local_episode_columns(
        episode_id,
        src_path=src_path,
        task_id=task_id,
        save_depth=save_depth,
        AgiBotWorld_CONFIG=AgiBotWorld_CONFIG,
    )
    if not columns:
        return episode_id, [], videos

    num_frames = len(next(iter(columns.values())))
    frames = [{key: value[i] for key, value in columns.items()} for i in range(num_frames)]
    return episode_id, frames, videos


def load_local_episode_columns(
    episode_id: int, src_path: str, task_id: int, save_depth: bool, AgiBotWorld_CONFIG: dict
) -> tuple[int, dict[str, np.ndarray], dict[str, Path]]:
    """Load local dataset and return columnar arrays keyed by LeRobot feature name."""
    ob_dir = Path(src_path) / f"observations/{task_id}/{episode_id}"
    proprio_dir = Path(src_path) / f"proprio_stats/{task_id}/{episode_id}"

    state = {}
    action = {}
    with h5py.File(proprio_dir / "proprio_stats.h5", "r") as f:
        for key in AgiBotWorld_CONFIG["states"]:
            state[f"observation.states.{key}"] = np.array(f["state/" + key.replace(".", "/")], dtype=np.float32)
        for key in AgiBotWorld_CONFIG["actions"]:
            action[f"actions.{key}"] = np.array(f["action/" + key.replace(".", "/")], dtype=np.float32)

        # HACK: agibot team forgot to pad or filter some of the values
        num_frames = len(next(iter(state.values())))
        for action_key, action_value in action.items():
            if 0 == len(action_value):
                print("0 action occurs, padding all with zeros later")
            elif len(action_value) < num_frames:
                state_key = action_key.replace("actions", "state").replace(".", "/")
                new_action_value = np.array(f[state_key], dtype=np.float32).copy()
                action_index_key = "/".join(list(action_key.replace("actions", "action").split(".")[:-1]) + ["index"])
                action_index = np.array(f[action_index_key])
                # agibot lost end index, replace it with joint
                if not action_index.size:
                    action_index_key = action_index_key.replace("end", "joint")
                    action_index = np.array(f[action_index_key])
                new_action_value[action_index] = action_value
                action[action_key] = new_action_value
            elif len(action_value) > num_frames:
                print("corrupt data, skipping")
                return episode_id, {}, {"dummy_video": Path("/path/to/no_exist")}

    if save_depth:
        depth_imgs = load_depths(ob_dir / "depth", "head_depth")
        assert num_frames == len(depth_imgs), "Number of images and states are not equal"

    columns: dict[str, np.ndarray] = {}
    for key, value in state.items():
        state_key = key.removeprefix("observation.states.")
        columns[key] = value if value.size else np.zeros(
            (num_frames, *AgiBotWorld_CONFIG["states"][state_key]["shape"]),
            dtype=AgiBotWorld_CONFIG["states"][state_key]["dtype"],
        )
    for key, value in action.items():
        action_key = key.removeprefix("actions.")
        columns[key] = value if value.size else np.zeros(
            (num_frames, *AgiBotWorld_CONFIG["actions"][action_key]["shape"]),
            dtype=AgiBotWorld_CONFIG["actions"][action_key]["dtype"],
        )
    if save_depth:
        columns["observation.images.head_depth"] = np.stack(depth_imgs, axis=0)

    videos = {
        f"observation.images.{key}": ob_dir / "videos" / f"{key}_color.mp4"
        if "sensor" not in key
        else ob_dir / "tactile" / f"{key}.mp4"  # HACK: handle tactile videos
        for key in AgiBotWorld_CONFIG["images"]
        if "depth" not in key
    }
    return episode_id, columns, videos
