from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _remove_tree(path: Path) -> bool:
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=False)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 HuggingFace / datasets 本地缓存")
    parser.add_argument(
        "--include-user-cache",
        action="store_true",
        help="同时清理用户目录下的 ~/.cache/huggingface",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    targets = [
        repo_root / ".hf",
        repo_root / ".tmp-hf-cache",
        repo_root / ".tmp-tests" / "hf-home",
    ]

    if args.include_user_cache:
        targets.append(Path.home() / ".cache" / "huggingface")

    removed: list[Path] = []
    missing: list[Path] = []
    for target in targets:
        if _remove_tree(target):
            removed.append(target)
        else:
            missing.append(target)

    print("Removed:")
    for path in removed:
        print(f"  {path}")

    print("Missing:")
    for path in missing:
        print(f"  {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
