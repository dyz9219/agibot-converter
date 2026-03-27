import argparse
import gc
import importlib
import shutil
import tempfile
from pathlib import Path

import numpy as np
import lerobot.datasets.lerobot_dataset as lerobot_dataset_module
from agibot_utils.agibot_utils import get_task_info, load_local_episode_columns
from agibot_utils.config import AgiBotWorld_TASK_TYPE
from agibot_utils.lerobot_utils import compute_episode_stats, generate_features_from_config
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.datasets.utils import validate_episode_buffer, validate_frame

_TORCH_MODULE = None
_TORCH_IMPORT_FAILED = False


def _is_torch_tensor(value) -> bool:
    global _TORCH_MODULE, _TORCH_IMPORT_FAILED
    module_name = getattr(value.__class__, "__module__", "")
    if not module_name.startswith("torch"):
        return False
    if _TORCH_IMPORT_FAILED:
        return False
    if _TORCH_MODULE is None:
        try:
            _TORCH_MODULE = importlib.import_module("torch")
        except Exception:
            _TORCH_IMPORT_FAILED = True
            return False
    return isinstance(value, _TORCH_MODULE.Tensor)


class AgiBotDataset(LeRobotDataset):
    def _save_episode_data(self, episode_buffer: dict) -> dict:
        ep_dict = {key: episode_buffer[key] for key in self.hf_features}
        ep_dataset = lerobot_dataset_module.datasets.Dataset.from_dict(
            ep_dict, features=self.hf_features, split="train"
        )
        if self.meta.image_keys:
            ep_dataset = lerobot_dataset_module.embed_images(ep_dataset)
        ep_num_frames = len(ep_dataset)

        if self.latest_episode is None:
            chunk_idx, file_idx = 0, 0
            global_frame_index = 0
            self._current_file_start_frame = 0
            if self.meta.episodes is not None and len(self.meta.episodes) > 0:
                latest_ep = self.meta.episodes[-1]
                global_frame_index = latest_ep["dataset_to_index"]
                chunk_idx = latest_ep["data/chunk_index"]
                file_idx = latest_ep["data/file_index"]
                chunk_idx, file_idx = lerobot_dataset_module.update_chunk_file_indices(
                    chunk_idx, file_idx, self.meta.chunks_size
                )
                self._current_file_start_frame = global_frame_index
        else:
            latest_ep = self.latest_episode
            chunk_idx = latest_ep["data/chunk_index"]
            file_idx = latest_ep["data/file_index"]
            global_frame_index = latest_ep["index"][-1] + 1

            latest_path = self.root / self.meta.data_path.format(chunk_index=chunk_idx, file_index=file_idx)
            latest_size_in_mb = lerobot_dataset_module.get_file_size_in_mb(latest_path)

            frames_in_current_file = global_frame_index - self._current_file_start_frame
            av_size_per_frame = latest_size_in_mb / frames_in_current_file if frames_in_current_file > 0 else 0

            if (
                latest_size_in_mb + av_size_per_frame * ep_num_frames >= self.meta.data_files_size_in_mb
                or self._writer_closed_for_reading
            ):
                chunk_idx, file_idx = lerobot_dataset_module.update_chunk_file_indices(
                    chunk_idx, file_idx, self.meta.chunks_size
                )
                self._close_writer()
                self._writer_closed_for_reading = False
                self._current_file_start_frame = global_frame_index

        ep_dict["data/chunk_index"] = chunk_idx
        ep_dict["data/file_index"] = file_idx

        path = self.root / self.meta.data_path.format(chunk_index=chunk_idx, file_index=file_idx)
        path.parent.mkdir(parents=True, exist_ok=True)

        table = ep_dataset.with_format("arrow")[:]
        if not self.writer:
            self.writer = lerobot_dataset_module.pq.ParquetWriter(
                path, schema=table.schema, compression="snappy", use_dictionary=True
            )
        self.writer.write_table(table)

        metadata = {
            "data/chunk_index": chunk_idx,
            "data/file_index": file_idx,
            "dataset_from_index": global_frame_index,
            "dataset_to_index": global_frame_index + ep_num_frames,
        }

        self.latest_episode = {**ep_dict, **metadata}
        self._lazy_loading = True
        self._recorded_frames += ep_num_frames

        return metadata

    def build_episode_data(self, frames: list[dict], task: str) -> dict:
        episode_data = self.create_episode_buffer()
        if not frames:
            return episode_data

        features = {key: value for key, value in self.features.items() if key in self.hf_features}
        normalized_frames: list[dict] = []
        timestamps: list[float] = []

        for index, frame in enumerate(frames):
            normalized: dict = {}
            for name, value in frame.items():
                normalized[name] = value.numpy() if _is_torch_tensor(value) else value

            frame_for_validation = {key: value for key, value in normalized.items() if key != "timestamp"}
            validate_frame({**frame_for_validation, "task": task}, features)
            normalized_frames.append(normalized)
            timestamps.append(normalized.get("timestamp", index / self.fps))

        episode_data["size"] = len(normalized_frames)
        episode_data["task"] = [task] * len(normalized_frames)
        episode_data["frame_index"] = np.arange(len(normalized_frames), dtype=np.int64)
        episode_data["timestamp"] = np.asarray(timestamps, dtype=np.float64)

        sample = normalized_frames[0]
        for key in sample:
            if key == "timestamp":
                continue
            if key not in self.features:
                raise ValueError(
                    f"An element of the frame is not in the features. '{key}' not in '{self.features.keys()}'."
                )
            episode_data[key] = np.stack([frame[key] for frame in normalized_frames]).squeeze()

        return episode_data

    def build_episode_data_from_columns(self, columns: dict[str, np.ndarray], task: str) -> dict:
        episode_data = self.create_episode_buffer()
        if not columns:
            return episode_data

        first_value = next(iter(columns.values()))
        num_frames = len(first_value)
        timestamps = np.arange(num_frames, dtype=np.float64) / self.fps

        episode_data["size"] = num_frames
        episode_data["task"] = [task] * num_frames
        episode_data["frame_index"] = np.arange(num_frames, dtype=np.int64)
        episode_data["timestamp"] = timestamps

        for key, value in columns.items():
            if key not in self.features:
                raise ValueError(
                    f"An element of the frame is not in the features. '{key}' not in '{self.features.keys()}'."
                )
            normalized = value.numpy() if _is_torch_tensor(value) else np.asarray(value)
            if len(normalized) != num_frames:
                raise ValueError(f"Inconsistent frame count for '{key}': {len(normalized)} != {num_frames}")
            episode_data[key] = normalized

        return episode_data

    def add_frame(self, frame: dict) -> None:
        """
        This function only adds the frame to the episode_buffer. Apart from images — which are written in a
        temporary directory — nothing is written to disk. To save those frames, the 'save_episode()' method
        then needs to be called.
        """
        # Convert torch to numpy if needed
        for name in frame:
            if _is_torch_tensor(frame[name]):
                frame[name] = frame[name].numpy()

        features = {key: value for key, value in self.features.items() if key in self.hf_features}  # remove video keys
        validate_frame(frame, features)

        if self.episode_buffer is None:
            self.episode_buffer = self.create_episode_buffer()

        # Automatically add frame_index and timestamp to episode buffer
        frame_index = self.episode_buffer["size"]
        timestamp = frame.pop("timestamp") if "timestamp" in frame else frame_index / self.fps
        self.episode_buffer["frame_index"].append(frame_index)
        self.episode_buffer["timestamp"].append(timestamp)
        self.episode_buffer["task"].append(frame.pop("task"))  # Remove task from frame after processing

        # Add frame features to episode_buffer
        for key, value in frame.items():
            if key not in self.features:
                raise ValueError(
                    f"An element of the frame is not in the features. '{key}' not in '{self.features.keys()}'."
                )

            self.episode_buffer[key].append(value)

        self.episode_buffer["size"] += 1

    def save_episode(self, videos: dict, action_config: list, episode_data: dict | None = None) -> None:
        """
        This will save to disk the current episode in self.episode_buffer.

        Args:
            episode_data (dict | None, optional): Dict containing the episode data to save. If None, this will
                save the current episode in self.episode_buffer, which is filled with 'add_frame'. Defaults to
                None.
        """
        episode_buffer = episode_data if episode_data is not None else self.episode_buffer

        validate_episode_buffer(episode_buffer, self.meta.total_episodes, self.features)

        # size and task are special cases that won't be added to hf_dataset
        episode_length = episode_buffer.pop("size")
        tasks = episode_buffer.pop("task")
        episode_tasks = list(set(tasks))
        episode_index = episode_buffer["episode_index"]

        episode_buffer["index"] = np.arange(self.meta.total_frames, self.meta.total_frames + episode_length)
        episode_buffer["episode_index"] = np.full((episode_length,), episode_index)

        # Update tasks and task indices with new tasks if any
        self.meta.save_episode_tasks(episode_tasks)

        # Given tasks in natural language, find their corresponding task indices
        episode_buffer["task_index"] = np.array([self.meta.get_task_index(task) for task in tasks])

        for key, ft in self.features.items():
            # index, episode_index, task_index are already processed above, and image and video
            # are processed separately by storing image path and frame info as meta data
            if key in ["index", "episode_index", "task_index"] or ft["dtype"] in ["video"]:
                continue
            if isinstance(episode_buffer[key], np.ndarray):
                continue
            episode_buffer[key] = np.stack(episode_buffer[key]).squeeze()

        for key in self.meta.video_keys:
            episode_buffer[key] = str(videos[key])  # PosixPath -> str

        ep_stats = compute_episode_stats(episode_buffer, self.features)

        ep_metadata = self._save_episode_data(episode_buffer)
        has_video_keys = len(self.meta.video_keys) > 0
        use_batched_encoding = self.batch_encoding_size > 1

        self.current_videos = videos
        if has_video_keys and not use_batched_encoding:
            for video_key in self.meta.video_keys:
                ep_metadata.update(self._save_episode_video(video_key, episode_index))

        # `meta.save_episode` be executed after encoding the videos
        # add action_config to current episode
        ep_metadata.update({"action_config": action_config})
        self.meta.save_episode(episode_index, episode_length, episode_tasks, ep_stats, ep_metadata)

        if has_video_keys and use_batched_encoding:
            # Check if we should trigger batch encoding
            self.episodes_since_last_encoding += 1
            if self.episodes_since_last_encoding == self.batch_encoding_size:
                start_ep = self.num_episodes - self.batch_encoding_size
                end_ep = self.num_episodes
                self._batch_save_episode_video(start_ep, end_ep)
                self.episodes_since_last_encoding = 0

        if not episode_data:
            # Reset episode buffer and clean up temporary images (if not already deleted during video encoding)
            self.clear_episode_buffer(delete_images=len(self.meta.image_keys) > 0)

    def _encode_temporary_episode_video(self, video_key: str, episode_index: int) -> Path:
        """
        Use ffmpeg to convert frames stored as png into mp4 videos.
        Note: `encode_video_frames` is a blocking call. Making it asynchronous shouldn't speedup encoding,
        since video encoding with ffmpeg is already using multithreading.
        """
        temp_path = Path(tempfile.mkdtemp(dir=self.root)) / f"{video_key}_{episode_index:03d}.mp4"
        shutil.copy(self.current_videos[video_key], temp_path)
        return temp_path


def get_all_tasks(src_path: Path, output_path: Path):
    json_files = src_path.glob("task_info/*.json")
    for json_file in json_files:
        local_dir = output_path / "agibotworld" / json_file.stem
        yield (json_file, local_dir.resolve())


def save_as_lerobot_dataset(agibot_world_config, task: tuple[Path, Path], save_depth):
    json_file, local_dir = task
    print(f"processing {json_file.stem}, saving to {local_dir}")
    src_path = json_file.parent.parent
    task_info = get_task_info(json_file)
    task_name = task_info[0]["task_name"]
    task_init_scene = task_info[0]["init_scene_text"]
    task_instruction = f"{task_name} | {task_init_scene}"
    task_id = json_file.stem.split("_")[-1]
    task_info = {episode["episode_id"]: episode for episode in task_info}

    features = generate_features_from_config(agibot_world_config)

    if local_dir.exists():
        shutil.rmtree(local_dir)

    if not save_depth:
        features.pop("observation.images.head_depth")

    dataset: AgiBotDataset = AgiBotDataset.create(
        repo_id=json_file.stem,
        root=local_dir,
        fps=30,
        robot_type="a2d",
        features=features,
    )

    all_subdir = [f.as_posix() for f in src_path.glob(f"observations/{task_id}/*") if f.is_dir()]

    all_subdir_eids = sorted([int(Path(path).name) for path in all_subdir])

    converted_episodes = 0
    skipped_episodes = 0
    for eid in all_subdir_eids:
        if eid not in task_info:
            print(f"{json_file.stem}, episode_{eid} not in task_info.json, skipping...")
            skipped_episodes += 1
            continue
        action_config = task_info[eid]["label_info"]["action_config"]
        raw_dataset = load_local_episode_columns(
            eid,
            src_path=src_path,
            task_id=task_id,
            save_depth=save_depth,
            AgiBotWorld_CONFIG=agibot_world_config,
        )
        _, episode_columns, videos = raw_dataset
        if not all([video_path.exists() for video_path in videos.values()]):
            print(f"{json_file.stem}, episode_{eid}: some of the videos does not exist, skipping...")
            skipped_episodes += 1
            continue

        episode_data = dataset.build_episode_data_from_columns(episode_columns, task_instruction)
        episode_size = int(episode_data.get("size", 0))
        try:
            dataset.save_episode(videos=videos, action_config=action_config, episode_data=episode_data)
        except Exception as e:
            print(f"{json_file.stem}, episode_{eid}: there are some corrupted mp4s\nException details: {str(e)}")
            dataset.episode_buffer = None
            skipped_episodes += 1
            continue
        gc.collect()
        print(f"process done for {json_file.stem}, episode_id {eid}, len {episode_size}")
        converted_episodes += 1

    # Do not silently report success for empty datasets.
    if converted_episodes == 0:
        raise RuntimeError(
            f"{json_file.stem}: no valid episodes converted "
            f"(converted={converted_episodes}, skipped={skipped_episodes})"
        )


def main(
    src_path: str,
    output_path: str,
    eef_type: str,
    task_ids: list,
    cpus_per_task: int,
    save_depth: bool,
    debug: bool = False,
):
    tasks = get_all_tasks(src_path, output_path)

    agibot_world_config, type_task_ids = (
        AgiBotWorld_TASK_TYPE[eef_type]["task_config"],
        AgiBotWorld_TASK_TYPE[eef_type]["task_ids"],
    )

    if eef_type == "gripper":
        remaining_ids = AgiBotWorld_TASK_TYPE["dexhand"]["task_ids"] + AgiBotWorld_TASK_TYPE["tactile"]["task_ids"]
        tasks = filter(lambda task: task[0].stem not in remaining_ids, tasks)
    else:
        tasks = filter(lambda task: task[0].stem in type_task_ids, tasks)

    if task_ids:
        tasks = filter(lambda task: task[0].stem in task_ids, tasks)

    if debug:
        save_as_lerobot_dataset(agibot_world_config, next(tasks), save_depth)
    else:
        ray = importlib.import_module("ray")
        runtime_env_cls = importlib.import_module("ray.runtime_env").RuntimeEnv
        runtime_env = runtime_env_cls(
            env_vars={"HDF5_USE_FILE_LOCKING": "FALSE", "HF_DATASETS_DISABLE_PROGRESS_BARS": "TRUE"}
        )
        ray.init(num_cpus=max(1, cpus_per_task), runtime_env=runtime_env)
        resources = ray.available_resources()
        cpus = int(resources["CPU"])

        print(f"Available CPUs: {cpus}, max_parallel_tasks: {cpus_per_task}")

        remote_task = ray.remote(save_as_lerobot_dataset).options(num_cpus=1)
        futures = []
        failed_tasks = []
        for task in tasks:
            futures.append((task[0].stem, remote_task.remote(agibot_world_config, task, save_depth)))

        for task, future in futures:
            try:
                ray.get(future)
            except Exception as e:
                print(f"Exception occurred for {task}")
                failed_tasks.append((task, str(e)))
                with open("output.txt", "a") as f:
                    f.write(f"{task}, exception details: {str(e)}\n")

        ray.shutdown()
        if failed_tasks:
            details = "; ".join([f"{t}:{err}" for t, err in failed_tasks[:5]])
            raise RuntimeError(f"{len(failed_tasks)} task(s) failed in any4 conversion: {details}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--eef-type", type=str, choices=["gripper", "dexhand", "tactile"], default="gripper")
    parser.add_argument("--task-ids", type=str, nargs="+", help="task_327 task_351 ...", default=[])
    parser.add_argument("--cpus-per-task", type=int, default=3)
    parser.add_argument("--save-depth", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    main(**vars(args))
