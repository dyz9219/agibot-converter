from __future__ import annotations

from pathlib import Path

from .models import SourceItem


_DATA_EXTS = {".h5", ".json", ".mp4"}


def discover_sources(input_path: Path) -> list[SourceItem]:
    if input_path.is_file():
        if input_path.suffix.lower() == ".zip":
            return [SourceItem(name=input_path.stem, source_path=input_path, is_zip=True)]
        if input_path.suffix.lower() in _DATA_EXTS:
            return [SourceItem(name=input_path.stem, source_path=input_path.parent, is_zip=False)]
        return []

    if not input_path.is_dir():
        return []

    if _looks_like_any4_dataset_dir(input_path):
        return [SourceItem(name=input_path.name, source_path=input_path, is_zip=False)]

    zip_files: list[SourceItem] = []
    extracted_dirs: list[SourceItem] = []

    for child in sorted(input_path.iterdir()):
        if child.is_file() and child.suffix.lower() == ".zip":
            zip_files.append(SourceItem(name=child.stem, source_path=child, is_zip=True))
        elif child.is_dir() and _looks_like_dataset_dir(child):
            extracted_dirs.append(SourceItem(name=child.name, source_path=child, is_zip=False))

    if zip_files or extracted_dirs:
        return [*zip_files, *extracted_dirs]

    if _looks_like_dataset_dir(input_path):
        return [SourceItem(name=input_path.name, source_path=input_path, is_zip=False)]

    return []


def _looks_like_dataset_dir(path: Path) -> bool:
    if _looks_like_any4_dataset_dir(path):
        return True

    try:
        for entry in path.iterdir():
            if entry.is_file() and entry.suffix.lower() in _DATA_EXTS:
                return True
    except OSError:
        return False
    return False


def _looks_like_any4_dataset_dir(path: Path) -> bool:
    task_info = path / "task_info"
    observations = path / "observations"
    if not task_info.is_dir() or not observations.is_dir():
        return False
    return any(p.suffix.lower() == ".json" for p in task_info.glob("*.json"))
