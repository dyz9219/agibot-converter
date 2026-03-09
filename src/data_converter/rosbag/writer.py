from __future__ import annotations

from pathlib import Path
from typing import Any

from rosbags.rosbag1 import Writer as Rosbag1Writer
from rosbags.rosbag2 import StoragePlugin, Writer as Rosbag2Writer
from rosbags.typesys import Stores, get_typestore


class HighLevelRosbagWriter:
    def __init__(self, output_dir: Path, bag_type: str) -> None:
        self.output_dir = output_dir
        self.bag_type = bag_type.strip().lower()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._is_ros1 = ".bag" in self.bag_type or "ros1" in self.bag_type
        if self._is_ros1:
            self.output_path = self.output_dir / "ros1_output.bag"
            self.typestore = get_typestore(Stores.ROS1_NOETIC)
            self._writer: Any = Rosbag1Writer(self.output_path)
        else:
            self.output_path = self.output_dir / "ros2_output"
            storage = StoragePlugin.MCAP if "mcap" in self.bag_type else StoragePlugin.SQLITE3
            self.typestore = get_typestore(Stores.ROS2_HUMBLE)
            self._writer = Rosbag2Writer(self.output_path, version=9, storage_plugin=storage)

    def __enter__(self) -> "HighLevelRosbagWriter":
        self._writer.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._writer.close()

    def add_topic(self, topic: str, msgtype: str):
        if self._is_ros1:
            return self._writer.add_connection(topic, msgtype, typestore=self.typestore)
        return self._writer.add_connection(topic, msgtype, typestore=self.typestore, serialization_format="cdr")

    def write_message(self, connection, msg, timestamp_ns: int, msgtype: str) -> None:
        if self._is_ros1:
            payload = self.typestore.serialize_ros1(msg, msgtype)
        else:
            payload = self.typestore.serialize_cdr(msg, msgtype)
        self._writer.write(connection, int(timestamp_ns), payload)
