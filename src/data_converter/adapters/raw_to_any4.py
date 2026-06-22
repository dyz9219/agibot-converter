from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np


@dataclass(slots=True)
class AdaptResult:
    prepared_root: Path
    input_kind: str
    adapter_used: bool
    workdir: Path | None
    warnings: list[str]


def detect_source_kind(path: Path) -> str:
    if _is_any4_dataset(path):
        return "any4"
    if _is_raw_source(path):
        return "raw"
    return "unknown"


def prepare_any4_source(
    source_path: Path,
    *,
    source_name: str,
    work_root: Path,
) -> AdaptResult:
    kind = detect_source_kind(source_path)
    if kind == "any4":
        return AdaptResult(
            prepared_root=source_path,
            input_kind="any4",
            adapter_used=False,
            workdir=None,
            warnings=[],
        )
    if kind != "raw":
        raise RuntimeError("输入既不是 any4 结构也不是原始 Agibot 包结构")

    work_root.mkdir(parents=True, exist_ok=True)
    run_root = work_root / f"{source_name}_adapted"
    if run_root.exists():
        shutil.rmtree(run_root, ignore_errors=True)
    run_root.mkdir(parents=True, exist_ok=False)

    raw_dir = _materialize_raw_dir(source_path, run_root)
    prepared_root = run_root / "any4_dataset"
    prepared_root.mkdir(parents=True, exist_ok=True)
    warnings = _build_min_any4_dataset(raw_dir, prepared_root, source_name)

    return AdaptResult(
        prepared_root=prepared_root,
        input_kind="raw",
        adapter_used=True,
        workdir=run_root,
        warnings=warnings,
    )


def _materialize_raw_dir(source_path: Path, run_root: Path) -> Path:
    if source_path.is_file() and source_path.suffix.lower() == ".zip":
        raw_root = run_root / "raw"
        raw_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(source_path, "r") as zf:
                zf.extractall(raw_root)
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError(
                "解压原始 zip 到适配目录失败。"
                f" zip={source_path}; raw_root={raw_root}; winerror={getattr(exc, 'winerror', '')}; err={exc}"
            ) from exc
        return _normalize_raw_dir(raw_root)
    return _normalize_raw_dir(source_path)


def _normalize_raw_dir(path: Path) -> Path:
    if (path / "aligned_joints.h5").exists() and (path / "state.json").exists():
        return path
    candidates: list[Path] = []
    try:
        for h5 in path.rglob("aligned_joints.h5"):
            parent = h5.parent
            if (parent / "state.json").exists():
                candidates.append(parent)
    except OSError as exc:
        raise RuntimeError(f"无法扫描原始输入目录: {path}; error={exc}") from exc

    unique: list[Path] = []
    seen: set[Path] = set()
    for c in sorted(candidates, key=lambda p: (len(p.parts), str(p).lower())):
        if c not in seen:
            seen.add(c)
            unique.append(c)

    if not unique:
        raise RuntimeError(f"原始输入目录不完整，缺少 aligned_joints.h5/state.json: {path}")

    # If multiple candidates exist, prefer the shallowest path to keep behavior deterministic.
    return unique[0]


def _build_min_any4_dataset(raw_dir: Path, dst_root: Path, source_name: str) -> list[str]:
    task_numeric = str(abs(hash(source_name)) % 900000 + 100000)
    task_id = f"task_{task_numeric}"
    episode_id = 1
    issues: list[str] = []
    init_scene_text = _read_platform_action_text(raw_dir) or "auto-adapted scene"

    # 1) task_info
    task_info_dir = dst_root / "task_info"
    task_info_dir.mkdir(parents=True, exist_ok=True)
    task_info = [
        {
            "episode_id": episode_id,
            "task_name": source_name,
            "init_scene_text": init_scene_text,
            "label_info": {"action_config": []},
        }
    ]
    (task_info_dir / f"{task_id}.json").write_text(
        json.dumps(task_info, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    # 2) observations videos
    obs_ep_dir = dst_root / "observations" / task_numeric / str(episode_id)
    videos_dir = obs_ep_dir / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    _build_videos(raw_dir, videos_dir, issues)

    # 3) proprio_stats
    proprio_dir = dst_root / "proprio_stats" / task_numeric / str(episode_id)
    proprio_dir.mkdir(parents=True, exist_ok=True)
    _build_proprio_stats(raw_dir / "aligned_joints.h5", proprio_dir / "proprio_stats.h5", issues)
    return issues


def _read_platform_action_text(raw_dir: Path) -> str:
    texts: list[str] = []
    for annotation_path in _iter_annotation_result_candidates(raw_dir):
        try:
            payload = json.loads(annotation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        texts.extend(_extract_action_texts(payload))
        if texts:
            break
    return "；".join(dict.fromkeys(texts))


def _iter_annotation_result_candidates(raw_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for base in (raw_dir, raw_dir.parent):
        path = base / "annotation_result.json"
        if path not in candidates and path.exists():
            candidates.append(path)
    return candidates


def _extract_action_texts(payload: object) -> list[str]:
    rows = payload if isinstance(payload, list) else [payload]
    keyed: list[tuple[int, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        text = row.get("action_text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        start_frame = row.get("start_frame", index)
        try:
            order = int(start_frame)
        except (TypeError, ValueError):
            order = index
        keyed.append((order, text))
    return [text for _, text in sorted(keyed, key=lambda item: item[0])]


def _build_videos(raw_dir: Path, videos_dir: Path, warnings: list[str]) -> None:
    raw_videos = sorted(p for p in raw_dir.glob("*.mp4") if p.is_file())
    if not raw_videos:
        raise RuntimeError(f"原始输入缺少 mp4 视频文件: {raw_dir}")
    video_map: dict[str, Path] = {}
    for src in raw_videos:
        key = _allocate_video_key(src.stem, video_map)
        video_map[key] = src

    for key, src in sorted(video_map.items()):
        _copy_or_link(src, videos_dir / f"{key}_color.mp4")


def _allocate_video_key(stem: str, existing: dict[str, Path]) -> str:
    key = _normalize_raw_video_key(stem)
    if key not in existing:
        return key
    suffix = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
    candidate = f"{key}_{suffix}"
    if candidate not in existing:
        return candidate
    index = 2
    while f"{candidate}_{index}" in existing:
        index += 1
    return f"{candidate}_{index}"


def _normalize_raw_video_key(stem: str) -> str:
    unicode_normalized = re.sub(r"[^\w]+", "_", stem.strip().lower(), flags=re.UNICODE).strip("_")
    ascii_source = (
        unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii").strip().lower()
    )
    ascii_normalized = re.sub(r"[^a-z0-9]+", "_", ascii_source).strip("_")
    base = ascii_normalized or "camera"
    # Keep downstream any4/LeRobot feature keys ASCII-safe. If non-ASCII content was
    # dropped during normalization, append a short hash to preserve uniqueness.
    if unicode_normalized != ascii_normalized:
        return f"{base}_{hashlib.sha1(stem.encode('utf-8')).hexdigest()[:8]}"
    return base


def _copy_or_link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
        return
    except OSError:
        pass
    try:
        shutil.copy2(src, dst)
    except OSError as exc:
        raise RuntimeError(
            "复制视频文件失败。"
            f" src={src}; dst={dst}; winerror={getattr(exc, 'winerror', '')}; err={exc}"
        ) from exc


def _build_proprio_stats(src_h5: Path, dst_h5: Path, warnings: list[str]) -> None:
    state_shapes: dict[str, tuple[int, ...]] = {
        "effector/position": (2,),
        "end/orientation": (2, 4),
        "end/position": (2, 3),
        "head/position": (2,),
        "joint/current_value": (16,),
        "joint/position": (16,),
        "robot/orientation": (4,),
        "robot/position": (3,),
        "waist/position": (2,),
    }
    action_shapes: dict[str, tuple[int, ...]] = {
        "effector/position": (2,),
        "end/orientation": (2, 4),
        "end/position": (2, 3),
        "head/position": (2,),
        "joint/position": (16,),
        "robot/velocity": (2,),
        "waist/position": (2,),
    }

    with h5py.File(src_h5, "r") as src, h5py.File(dst_h5, "w") as dst:
        num_frames = _detect_num_frames(src)
        cache: dict[str, np.ndarray] = {}
        for rel, shape in state_shapes.items():
            _write_dataset(
                dst,
                f"state/{rel}",
                _read_or_default(src, f"state/{rel}", num_frames, shape, warnings, cache),
            )
        for rel, shape in action_shapes.items():
            _write_dataset(
                dst,
                f"action/{rel}",
                _read_or_default(src, f"action/{rel}", num_frames, shape, warnings, cache),
            )


def _detect_num_frames(src: h5py.File) -> int:
    if "timestamp" in src:
        arr = np.array(src["timestamp"], dtype=np.float32)
        if arr.size > 0:
            return int(arr.shape[0])
    for k in ("state/end/position", "action/end/position", "state/joint/position", "action/joint/position"):
        if k in src:
            arr = np.array(src[k], dtype=np.float32)
            if arr.ndim >= 1 and arr.shape[0] > 0:
                return int(arr.shape[0])
    raise RuntimeError("无法从 aligned_joints.h5 推断帧数")


def _read_or_default(
    src: h5py.File,
    key: str,
    num_frames: int,
    target_shape: tuple[int, ...],
    warnings: list[str],
    cache: dict[str, np.ndarray],
) -> np.ndarray:
    arr = _resolve_raw_array(src, key, cache)
    if key in {"state/effector/position", "action/effector/position"}:
        derived = _derive_effector_from_joint(key, num_frames, src, cache)
        split = _derive_effector_from_split_fields(key, num_frames, src, cache)
        normalized = _normalize_raw_array(key, arr, num_frames, target_shape, src, cache)
        if normalized is not None:
            if derived is not None and arr is not None:
                warnings.append(f"数据键 {key} 已优先采用 joint.position 派生值，忽略原始 effector 数据")
            elif split is not None and arr is None:
                warnings.append(f"数据键 {key} 缺失，已由 left/right effector 字段重建")
            elif derived is not None and arr is None:
                warnings.append(f"数据键 {key} 缺失，已从 joint.position 重建训练结构")
            return normalized
        if arr is not None:
            warnings.append(f"数据键 {key} 形状 {arr.shape} 与期望 {(num_frames, *target_shape)} 不一致，填充零值")
        else:
            warnings.append(f"数据键 {key} 缺失，填充零值")
        return np.zeros((num_frames, *target_shape), dtype=np.float32)

    if arr is not None:
        normalized = _normalize_raw_array(key, arr, num_frames, target_shape, src, cache)
        if normalized is not None:
            if arr.shape != normalized.shape:
                warnings.append(
                    f"数据键 {key} 形状 {arr.shape} 与期望 {(num_frames, *target_shape)} 不一致，已重排到训练结构"
                )
            return normalized
        warnings.append(f"数据键 {key} 形状 {arr.shape} 与期望 {(num_frames, *target_shape)} 不一致，填充零值")
    else:
        warnings.append(f"数据键 {key} 缺失，填充零值")
    return np.zeros((num_frames, *target_shape), dtype=np.float32)


def _resolve_raw_array(src: h5py.File, key: str, cache: dict[str, np.ndarray]) -> np.ndarray | None:
    if key in cache:
        return cache[key]
    if key not in src:
        return None
    arr = np.array(src[key], dtype=np.float32)
    cache[key] = arr
    return arr


def _normalize_raw_array(
    key: str,
    arr: np.ndarray | None,
    num_frames: int,
    target_shape: tuple[int, ...],
    src: h5py.File,
    cache: dict[str, np.ndarray],
) -> np.ndarray | None:
    if arr is not None and key not in {"state/effector/position", "action/effector/position"} and arr.ndim >= 1 and arr.shape[0] == num_frames and tuple(arr.shape[1:]) == target_shape:
        return arr
    if key in {"state/effector/position", "action/effector/position"}:
        resolved = _resolve_effector_array(key, num_frames, src, cache)
        if resolved is not None and tuple(resolved.shape[1:]) == target_shape:
            return resolved
        if arr is not None and arr.ndim >= 1 and arr.shape[0] == num_frames and tuple(arr.shape[1:]) == target_shape:
            return arr
    if arr is not None and arr.ndim == 2 and len(target_shape) == 1 and arr.shape[0] == num_frames:
        if key in {"state/joint/position", "action/joint/position", "state/joint/current_value"} and arr.shape[1] >= target_shape[0]:
            return arr[:, : target_shape[0]].astype(np.float32, copy=False)
        if key in {"state/joint/position", "action/joint/position", "state/joint/current_value"} and arr.shape[1] < target_shape[0]:
            out = np.zeros((num_frames, target_shape[0]), dtype=np.float32)
            out[:, : arr.shape[1]] = arr
            effector = _resolve_effector_array(_joint_to_effector_key(key), num_frames, src, cache)
            if effector is not None and arr.shape[1] + effector.shape[1] <= target_shape[0]:
                out[:, arr.shape[1] : arr.shape[1] + effector.shape[1]] = effector
            return out
    if arr is not None and arr.ndim == 1 and len(target_shape) == 1 and arr.shape[0] == num_frames and target_shape[0] == 2:
        out = np.zeros((num_frames, 2), dtype=np.float32)
        out[:, 0] = arr
        return out

    return None


def _joint_to_effector_key(key: str) -> str:
    prefix = key.split('/', 1)[0]
    return f"{prefix}/effector/position"


def _resolve_effector_array(
    key: str,
    num_frames: int,
    src: h5py.File,
    cache: dict[str, np.ndarray],
) -> np.ndarray | None:
    derived = _derive_effector_from_joint(key, num_frames, src, cache)
    if derived is not None:
        return derived

    arr = _resolve_raw_array(src, key, cache)
    normalized = _normalize_effector_array(arr, num_frames)
    if normalized is not None:
        return normalized

    return _derive_effector_from_split_fields(key, num_frames, src, cache)


def _normalize_effector_array(arr: np.ndarray | None, num_frames: int) -> np.ndarray | None:
    if arr is None:
        return None
    if arr.ndim == 2 and arr.shape == (num_frames, 2):
        return arr.astype(np.float32, copy=False)
    if arr.ndim == 1 and arr.shape[0] == num_frames:
        out = np.zeros((num_frames, 2), dtype=np.float32)
        out[:, 0] = arr.astype(np.float32, copy=False)
        return out
    if arr.ndim == 2 and arr.shape == (num_frames, 1):
        out = np.zeros((num_frames, 2), dtype=np.float32)
        out[:, 0] = arr[:, 0].astype(np.float32, copy=False)
        return out
    return None


def _derive_effector_from_split_fields(
    key: str,
    num_frames: int,
    src: h5py.File,
    cache: dict[str, np.ndarray],
) -> np.ndarray | None:
    prefix = key.split('/', 1)[0]
    left = _resolve_raw_array(src, f"{prefix}/left_effector/position", cache)
    right = _resolve_raw_array(src, f"{prefix}/right_effector/position", cache)
    left_col = _normalize_effector_channel(left, num_frames)
    right_col = _normalize_effector_channel(right, num_frames)
    if left_col is None or right_col is None:
        return None
    return np.concatenate([left_col, right_col], axis=1)


def _normalize_effector_channel(arr: np.ndarray | None, num_frames: int) -> np.ndarray | None:
    if arr is None:
        return None
    if arr.ndim == 2 and arr.shape == (num_frames, 1):
        return arr.astype(np.float32, copy=False)
    if arr.ndim == 1 and arr.shape[0] == num_frames:
        return arr.reshape(num_frames, 1).astype(np.float32, copy=False)
    return None


def _derive_effector_from_joint(
    key: str,
    num_frames: int,
    src: h5py.File,
    cache: dict[str, np.ndarray],
) -> np.ndarray | None:
    joint_key = key.replace("effector", "joint")
    joint_arr = _resolve_raw_array(src, joint_key, cache)
    if joint_arr is None or joint_arr.ndim != 2 or joint_arr.shape[0] != num_frames or joint_arr.shape[1] < 16:
        return None
    return joint_arr[:, 14:16].astype(np.float32, copy=False)


def _write_dataset(dst: h5py.File, key: str, value: np.ndarray) -> None:
    parent = dst
    parts = key.split("/")
    for p in parts[:-1]:
        parent = parent.require_group(p)
    parent.create_dataset(parts[-1], data=value)


def _is_any4_dataset(path: Path) -> bool:
    if path.is_file():
        return False
    task_info = path / "task_info"
    observations = path / "observations"
    return task_info.is_dir() and observations.is_dir() and any(task_info.glob("*.json"))


def _is_raw_source(path: Path) -> bool:
    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = [n.replace("\\", "/").lower() for n in zf.namelist()]
        except OSError:
            return False
        has_h5 = any(name.endswith("/aligned_joints.h5") or name == "aligned_joints.h5" for name in names)
        has_state = any(name.endswith("/state.json") or name == "state.json" for name in names)
        return has_h5 and has_state
    if path.is_file():
        return False
    if (path / "aligned_joints.h5").exists() and (path / "state.json").exists():
        return True
    for h5 in path.rglob("aligned_joints.h5"):
        if (h5.parent / "state.json").exists():
            return True
    return False
