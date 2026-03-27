from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect parquet columns and sample values.")
    parser.add_argument("parquet_path", type=Path, help="Path to the parquet file.")
    parser.add_argument("--column", action="append", default=[], help="Column name to inspect. Repeatable.")
    parser.add_argument("--rows", type=int, default=3, help="Number of sample rows to print per column.")
    parser.add_argument(
        "--show-image-info",
        action="store_true",
        help="When inspecting struct<bytes,path> image columns, decode bytes and print image size/mode.",
    )
    return parser


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        preview = value[:24]
        return f"<bytes: {len(value)} bytes, hex:{preview.hex()}>"
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _maybe_decode_image_info(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    payload = value.get("bytes")
    if not isinstance(payload, (bytes, bytearray)):
        return None
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(bytes(payload)))
        return {"size": list(image.size), "mode": image.mode}
    except Exception as exc:  # noqa: BLE001
        return {"decode_error": str(exc)}


def main() -> int:
    args = _build_parser().parse_args()
    table = pq.read_table(args.parquet_path)

    print("Columns:")
    for name in table.column_names:
        print(f"- {name}")

    selected_columns = args.column or list(table.column_names)
    row_count = max(1, args.rows)

    for column_name in selected_columns:
        if column_name not in table.column_names:
            print(f"\n[{column_name}] not found")
            continue

        values = table[column_name].to_pylist()
        print(f"\n[{column_name}]")
        for index, value in enumerate(values[:row_count]):
            print(f"row {index}: {json.dumps(_json_safe(value), ensure_ascii=False)}")
            if args.show_image_info:
                info = _maybe_decode_image_info(value)
                if info is not None:
                    print(f"row {index} image_info: {json.dumps(info, ensure_ascii=False)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
