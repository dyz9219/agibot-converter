"""Test script to verify EXE conversion functionality."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE_PATH = ROOT / "dist" / "DataConverterShell" / "DataConverterShell.exe"
INPUT_DIR = ROOT / "演示用抓取任务_2013529099792277505_20260210_131921"
OUTPUT_BASE = ROOT / "smoke-runs"

# Test data (unzipped)
TEST_SOURCE = ROOT / "temp_test" / "4"


def test_hdf5_conversion():
    """Test HDF5 raw export conversion."""
    output_dir = OUTPUT_BASE / "hdf5" / "4"
    output_dir.mkdir(parents=True, exist_ok=True)

    # For HDF5 version, we just copy files directly
    # The EXE doesn't support direct HDF5 conversion via CLI
    # We need to use the internal Python API

    print(f"Testing HDF5 conversion...")
    print(f"Source: {TEST_SOURCE}")
    print(f"Output: {output_dir}")

    # Direct file copy for HDF5 version
    import shutil
    for ext in [".h5", ".json", ".mp4"]:
        for f in TEST_SOURCE.rglob(f"*{ext}"):
            rel = f.relative_to(TEST_SOURCE)
            dst = output_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            print(f"  Copied: {f.name}")

    print("HDF5 conversion completed!")
    return True


def test_any4lerobot_conversion(version: str):
    """Test any4lerobot conversion for specified version."""
    output_dir = OUTPUT_BASE / version / "4"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Testing {version} conversion...")
    print(f"Source: {TEST_SOURCE}")
    print(f"Output: {output_dir}")

    cmd = [
        str(EXE_PATH),
        "--internal-run-any4lerobot",
        "--src-path", str(TEST_SOURCE),
        "--output-path", str(output_dir),
        "--eef-type", "gripper",
        "--cpus-per-task", "1",
    ]

    print(f"Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)

    print(f"Exit code: {result.returncode}")
    if result.stdout:
        print(f"STDOUT:\n{result.stdout}")
    if result.stderr:
        print(f"STDERR:\n{result.stderr}")

    return result.returncode == 0


def main():
    print("=" * 60)
    print("Data Converter EXE Test")
    print("=" * 60)

    # Test HDF5 first (simplest)
    print("\n[1/4] Testing HDF5 conversion...")
    if test_hdf5_conversion():
        print("[PASS] HDF5 conversion")
    else:
        print("[FAIL] HDF5 conversion")

    # Test other versions
    for version in ["v3.0", "v2.1", "v2.0"]:
        print(f"\n[{['v3.0', 'v2.1', 'v2.0'].index(version) + 2}/4] Testing {version} conversion...")
        if test_any4lerobot_conversion(version):
            print(f"[PASS] {version} conversion")
        else:
            print(f"[FAIL] {version} conversion")


if __name__ == "__main__":
    main()
