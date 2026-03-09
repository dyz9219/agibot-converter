from __future__ import annotations

from .models import ConversionOptions, TargetKind


def output_suffix(options: ConversionOptions) -> str:
    if options.target is TargetKind.LEROBOT:
        version = options.lerobot_version.strip().lower()
        if version == "hdf5":
            return "hdf5_raw"
        if version in {"v3.0", "v3"}:
            return "lerobot_v30"
        if version in {"v2.1", "v21"}:
            return "lerobot_v21"
        if version in {"v2.0", "v2"}:
            return "lerobot_v20"
        return "lerobot_unknown"

    bag = options.bag_type.strip().lower()
    if "mcap" in bag:
        return "rosbag_mcap"
    if "db3" in bag:
        return "rosbag_db3"
    if ".bag" in bag or "ros1" in bag:
        return "rosbag_bag"
    return "rosbag_unknown"

