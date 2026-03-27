from concurrent.futures import ThreadPoolExecutor

import av
import numpy as np

DEFAULT_QUANTILES = [0.01, 0.10, 0.50, 0.90, 0.99]


def sample_indices(data_len: int) -> list[int]:
    num_samples = estimate_num_samples(data_len)
    return np.round(np.linspace(0, data_len - 1, num_samples)).astype(int).tolist()


def estimate_num_samples(data_len: int) -> int:
    return min(20, max(1, data_len))


def auto_downsample_height_width(img: np.ndarray, target_size: int = 150, max_size_threshold: int = 300):
    _, height, width = img.shape

    if max(width, height) < max_size_threshold:
        return img

    downsample_factor = int(width / target_size) if width > height else int(height / target_size)
    return img[:, ::downsample_factor, ::downsample_factor]


class RunningQuantileStats:
    def __init__(self, quantile_list: list[float] | None = None, num_quantile_bins: int = 5000):
        self._count = 0
        self._mean = None
        self._mean_of_squares = None
        self._min = None
        self._max = None
        self._histograms = None
        self._bin_edges = None
        self._num_quantile_bins = num_quantile_bins
        self._quantile_list = quantile_list or DEFAULT_QUANTILES
        self._quantile_keys = [f"q{int(q * 100):02d}" for q in self._quantile_list]

    def update(self, batch: np.ndarray) -> None:
        batch = batch.reshape(-1, batch.shape[-1])
        num_elements, vector_length = batch.shape

        if self._count == 0:
            self._mean = np.mean(batch, axis=0)
            self._mean_of_squares = np.mean(batch**2, axis=0)
            self._min = np.min(batch, axis=0)
            self._max = np.max(batch, axis=0)
            self._histograms = [np.zeros(self._num_quantile_bins) for _ in range(vector_length)]
            self._bin_edges = [
                np.linspace(self._min[i] - 1e-10, self._max[i] + 1e-10, self._num_quantile_bins + 1)
                for i in range(vector_length)
            ]
        else:
            if vector_length != self._mean.size:
                raise ValueError("The length of new vectors does not match the initialized vector length.")

            new_max = np.max(batch, axis=0)
            new_min = np.min(batch, axis=0)
            max_changed = np.any(new_max > self._max)
            min_changed = np.any(new_min < self._min)
            self._max = np.maximum(self._max, new_max)
            self._min = np.minimum(self._min, new_min)

            if max_changed or min_changed:
                self._adjust_histograms()

        self._count += num_elements
        batch_mean = np.mean(batch, axis=0)
        batch_mean_of_squares = np.mean(batch**2, axis=0)
        self._mean += (batch_mean - self._mean) * (num_elements / self._count)
        self._mean_of_squares += (batch_mean_of_squares - self._mean_of_squares) * (num_elements / self._count)
        self._update_histograms(batch)

    def get_statistics(self) -> dict[str, np.ndarray]:
        if self._count < 2:
            raise ValueError("Cannot compute statistics for less than 2 vectors.")

        variance = self._mean_of_squares - self._mean**2
        stddev = np.sqrt(np.maximum(0, variance))
        stats = {
            "min": self._min.copy(),
            "max": self._max.copy(),
            "mean": self._mean.copy(),
            "std": stddev,
            "count": np.array([self._count]),
        }
        quantile_results = self._compute_quantiles()
        for i, q in enumerate(self._quantile_keys):
            stats[q] = quantile_results[i]
        return stats

    def _adjust_histograms(self):
        for i in range(len(self._histograms)):
            old_edges = self._bin_edges[i]
            old_hist = self._histograms[i]
            padding = (self._max[i] - self._min[i]) * 1e-10
            new_edges = np.linspace(self._min[i] - padding, self._max[i] + padding, self._num_quantile_bins + 1)
            new_hist = np.zeros(self._num_quantile_bins)

            old_centers = (old_edges[:-1] + old_edges[1:]) / 2
            for old_center, count in zip(old_centers, old_hist, strict=False):
                if count > 0:
                    bin_idx = np.searchsorted(new_edges, old_center) - 1
                    bin_idx = max(0, min(bin_idx, self._num_quantile_bins - 1))
                    new_hist[bin_idx] += count

            self._histograms[i] = new_hist
            self._bin_edges[i] = new_edges

    def _update_histograms(self, batch: np.ndarray) -> None:
        for i in range(batch.shape[1]):
            hist, _ = np.histogram(batch[:, i], bins=self._bin_edges[i])
            self._histograms[i] += hist

    def _compute_quantiles(self) -> list[np.ndarray]:
        results = []
        for q in self._quantile_list:
            target_count = q * self._count
            q_values = []
            for hist, edges in zip(self._histograms, self._bin_edges, strict=True):
                q_values.append(self._compute_single_quantile(hist, edges, target_count))
            results.append(np.array(q_values))
        return results

    def _compute_single_quantile(self, hist: np.ndarray, edges: np.ndarray, target_count: float) -> float:
        cumsum = np.cumsum(hist)
        idx = np.searchsorted(cumsum, target_count)
        if idx == 0:
            return edges[0]
        if idx >= len(cumsum):
            return edges[-1]
        count_before = cumsum[idx - 1]
        count_in_bin = cumsum[idx] - count_before
        if count_in_bin == 0:
            return edges[idx]
        fraction = (target_count - count_before) / count_in_bin
        return edges[idx] + fraction * (edges[idx + 1] - edges[idx])


def _prepare_array_for_stats(array: np.ndarray, axis: int | tuple[int, ...] | None) -> tuple[np.ndarray, int]:
    if axis == (0, 2, 3):
        batch_size, channels, height, width = array.shape
        reshaped = array.transpose(0, 2, 3, 1).reshape(-1, channels)
        return reshaped, batch_size
    if axis == 0 or axis == (0,):
        reshaped = array if array.ndim > 1 else array.reshape(-1, 1)
        return reshaped, array.shape[0]
    if axis == (1,):
        return array.T, array.shape[1]
    if axis is None:
        reshaped = array.reshape(-1, 1)
        return reshaped, array.shape[0] if array.ndim > 0 else 1
    raise ValueError(f"Unsupported axis configuration: {axis}")


def _reshape_single_stat(
    value: np.ndarray,
    axis: int | tuple[int, ...] | None,
    keepdims: bool,
    original_shape: tuple[int, ...],
) -> np.ndarray:
    if not keepdims:
        return value
    if axis == (0, 2, 3):
        return value.reshape(1, original_shape[1], 1, 1)
    if axis == 0 or axis == (0,):
        if len(original_shape) == 1:
            return value.reshape(1)
        return value.reshape((1, *original_shape[1:]))
    if axis == (1,):
        return value.reshape((original_shape[0], 1))
    if axis is None:
        return value.reshape((1,) * len(original_shape))
    raise ValueError(f"Unsupported axis configuration: {axis}")


def _reshape_stats_by_axis(
    stats: dict[str, np.ndarray],
    axis: int | tuple[int, ...] | None,
    keepdims: bool,
    original_shape: tuple[int, ...],
) -> dict[str, np.ndarray]:
    if axis == (1,) and not keepdims:
        return stats
    result = {}
    for key, value in stats.items():
        result[key] = value if key == "count" else _reshape_single_stat(value, axis, keepdims, original_shape)
    return result


def _compute_basic_stats(
    array: np.ndarray, sample_count: int, quantile_list: list[float] | None = None
) -> dict[str, np.ndarray]:
    quantile_list = quantile_list or DEFAULT_QUANTILES
    quantile_list_keys = [f"q{int(q * 100):02d}" for q in quantile_list]
    stats = {
        "min": np.min(array, axis=0),
        "max": np.max(array, axis=0),
        "mean": np.mean(array, axis=0),
        "std": np.std(array, axis=0),
        "count": np.array([sample_count]),
    }
    for q in quantile_list_keys:
        stats[q] = stats["mean"].copy()
    return stats


def get_feature_stats(
    array: np.ndarray,
    axis: int | tuple[int, ...] | None,
    keepdims: bool,
    quantile_list: list[float] | None = None,
) -> dict[str, np.ndarray]:
    quantile_list = quantile_list or DEFAULT_QUANTILES
    original_shape = array.shape
    reshaped, sample_count = _prepare_array_for_stats(array, axis)
    if reshaped.shape[0] < 2:
        stats = _compute_basic_stats(reshaped, sample_count, quantile_list)
    else:
        running_stats = RunningQuantileStats(quantile_list=quantile_list)
        running_stats.update(reshaped)
        stats = running_stats.get_statistics()
        stats["count"] = np.array([sample_count])
    return _reshape_stats_by_axis(stats, axis, keepdims, original_shape)


def generate_features_from_config(AgiBotWorld_CONFIG):
    features = {}
    for key, value in AgiBotWorld_CONFIG["images"].items():
        features[f"observation.images.{key}"] = value
    for key, value in AgiBotWorld_CONFIG["states"].items():
        features[f"observation.states.{key}"] = value
    for key, value in AgiBotWorld_CONFIG["actions"].items():
        features[f"actions.{key}"] = value
    return features


def sample_images(input):
    if type(input) is str:
        video_path = input
        images = _sample_video_frames(video_path)
    elif type(input) is np.ndarray:
        frames_array = input[:, None, :, :]  # Shape: [T, C, H, W]
        sampled_indices = sample_indices(len(frames_array))
        images = None
        for i, idx in enumerate(sampled_indices):
            img = frames_array[idx]
            img = auto_downsample_height_width(img)

            if images is None:
                images = np.empty((len(sampled_indices), *img.shape), dtype=np.uint8)

            images[i] = img

    return images


def _sample_video_frames(video_path: str) -> np.ndarray:
    with av.open(video_path) as container:
        stream = container.streams.video[0]
        _configure_video_decoder(stream)
        total_frames = int(stream.frames or 0)
        if total_frames <= 0:
            decoded = [_frame_to_chw(frame) for frame in container.decode(stream)]
            if not decoded:
                raise RuntimeError(f"Video has no decodable frames: {video_path}")
            sampled_indices = sample_indices(len(decoded))
            images = None
            for i, idx in enumerate(sampled_indices):
                img = auto_downsample_height_width(decoded[idx])
                if images is None:
                    images = np.empty((len(sampled_indices), *img.shape), dtype=np.uint8)
                images[i] = img
            return images

        sampled_indices = sample_indices(total_frames)
        sampled_set = set(sampled_indices)
        images = None
        write_index = 0
        for frame_index, frame in enumerate(container.decode(stream)):
            if frame_index not in sampled_set:
                continue
            img = auto_downsample_height_width(_frame_to_chw(frame))
            if images is None:
                images = np.empty((len(sampled_indices), *img.shape), dtype=np.uint8)
            images[write_index] = img
            write_index += 1
            if write_index == len(sampled_indices):
                break

        if images is None or write_index != len(sampled_indices):
            raise RuntimeError(
                f"Failed to sample expected frames from video: {video_path}; "
                f"expected={len(sampled_indices)} got={write_index}"
            )
        return images


def _configure_video_decoder(stream) -> None:
    codec_context = getattr(stream, "codec_context", None)
    if codec_context is None:
        return
    codec_context.thread_type = "AUTO"
    codec_context.thread_count = 0


def _frame_to_chw(frame: av.VideoFrame) -> np.ndarray:
    rgb = frame.to_rgb().to_ndarray()
    return np.transpose(rgb, (2, 0, 1))


def _compute_non_visual_stats(data: np.ndarray) -> dict[str, np.ndarray]:
    return get_feature_stats(data, axis=0, keepdims=data.ndim == 1)


def _compute_visual_stats(key: str, data: str | np.ndarray) -> tuple[str, dict[str, np.ndarray]]:
    ep_ft_array = sample_images(data)
    stats = get_feature_stats(ep_ft_array, axis=(0, 2, 3), keepdims=True)
    value_norm = 1.0 if "depth" in key else 255.0
    normalized = {k: v if k == "count" else np.squeeze(v / value_norm, axis=0) for k, v in stats.items()}
    return key, normalized


def compute_episode_stats(episode_data: dict[str, list[str] | np.ndarray], features: dict) -> dict:
    visual_items: list[tuple[str, str | np.ndarray]] = []
    non_visual_stats: dict[str, dict[str, np.ndarray]] = {}
    for key, data in episode_data.items():
        dtype = features[key]["dtype"]
        if dtype == "string":
            continue  # HACK: we should receive np.arrays of strings
        if dtype in ["image", "video"]:
            visual_items.append((key, data))
            continue
        non_visual_stats[key] = _compute_non_visual_stats(data)

    if len(visual_items) == 1:
        visual_results = [_compute_visual_stats(*visual_items[0])]
    elif len(visual_items) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(visual_items))) as executor:
            visual_results = list(executor.map(lambda item: _compute_visual_stats(*item), visual_items))
    else:
        visual_results = []

    visual_stats = dict(visual_results)
    ep_stats = {}
    for key in episode_data:
        dtype = features[key]["dtype"]
        if dtype == "string":
            continue
        if dtype in ["image", "video"]:
            ep_stats[key] = visual_stats[key]
        else:
            ep_stats[key] = non_visual_stats[key]

    return ep_stats
