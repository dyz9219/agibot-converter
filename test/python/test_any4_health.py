from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from data_converter import any4_health
from data_converter import any4lerobot_locator


class Any4HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        any4_health._CACHE.clear()

    def test_combines_bundled_and_external_diagnostics(self) -> None:
        root = Path("C:/fake/any4lerobot")
        bundled = any4_health.RuntimeCheckResult(
            ok=False,
            mode="bundled",
            root=str(root),
            python="",
            missing=["agibot2lerobot_import"],
            diagnostic="mode=bundled; root=C:/fake/any4lerobot; python=; missing=agibot2lerobot_import; bundled_error=ModuleNotFoundError:torch",
        )
        external = any4_health.RuntimeCheckResult(
            ok=False,
            mode="none",
            root=str(root),
            python="",
            missing=["python"],
            diagnostic="mode=none; root=C:/fake/any4lerobot; python=; missing=python; external_error=python_not_found",
        )

        with (
            patch("data_converter.any4_health.find_any4lerobot_root", return_value=root),
            patch("data_converter.any4_health.find_any4_python_for_version", return_value=None),
            patch("data_converter.any4_health._check_bundled_runtime", return_value=bundled),
            patch("data_converter.any4_health._check_external_runtime", return_value=external),
            patch("data_converter.any4_health.time.monotonic", return_value=1.0),
        ):
            result = any4_health.check_any4_runtime("v3.0")

        self.assertFalse(result.ok)
        self.assertEqual(result.mode, "none")
        self.assertIn("agibot2lerobot_import", result.missing)
        self.assertIn("python", result.missing)
        self.assertIn("bundled_error=", result.diagnostic)
        self.assertIn("external_error=", result.diagnostic)

    def test_bundled_success_short_circuits_external(self) -> None:
        root = Path("C:/fake/any4lerobot")
        bundled_ok = any4_health.RuntimeCheckResult(
            ok=True,
            mode="bundled",
            root=str(root),
            python="",
            missing=[],
            diagnostic="mode=bundled; root=C:/fake/any4lerobot; python=",
        )

        with (
            patch("data_converter.any4_health.find_any4lerobot_root", return_value=root),
            patch("data_converter.any4_health.find_any4_python_for_version", return_value=None),
            patch("data_converter.any4_health._check_bundled_runtime", return_value=bundled_ok),
            patch("data_converter.any4_health._check_external_runtime") as ext_probe,
            patch("data_converter.any4_health.time.monotonic", return_value=2.0),
        ):
            result = any4_health.check_any4_runtime("v3.0")

        self.assertTrue(result.ok)
        ext_probe.assert_not_called()

    def test_locator_prefers_repo_any4_before_parent_sibling(self) -> None:
        fake_file = Path(r"D:\workspace\work\bwy\agibot-converter\src\data_converter\any4lerobot_locator.py")

        with patch.object(any4lerobot_locator, "__file__", str(fake_file)):
            roots = any4lerobot_locator._candidate_roots()

        repo_root = fake_file.resolve().parents[2] / "any4lerobot"
        sibling_root = fake_file.resolve().parents[3] / "any4lerobot"
        self.assertLess(roots.index(repo_root), roots.index(sibling_root))

    def test_lightweight_psutil_probe_avoids_ray_submodule_spec_imports(self) -> None:
        temp_root = Path(".tmp-tests")
        temp_root.mkdir(exist_ok=True)
        tmp_path = temp_root / f"health-probe-{uuid.uuid4().hex}"
        psutil_dir = tmp_path / "psutil"
        ray_dir = tmp_path / "ray"
        thirdparty_psutil_dir = ray_dir / "thirdparty_files" / "psutil"
        psutil_dir.mkdir(parents=True, exist_ok=True)
        thirdparty_psutil_dir.mkdir(parents=True, exist_ok=True)
        (psutil_dir / "_psutil_windows.pyd").write_text("", encoding="utf-8")
        (thirdparty_psutil_dir / "_psutil_windows.pyd").write_text("", encoding="utf-8")

        def fake_find_spec(name: str):
            if name == "psutil":
                return SimpleNamespace(
                    origin=str(psutil_dir / "__init__.py"),
                    submodule_search_locations=[str(psutil_dir)],
                )
            if name == "psutil._psutil_windows":
                return SimpleNamespace(origin=str(psutil_dir / "_psutil_windows.pyd"))
            if name == "ray":
                return SimpleNamespace(
                    origin=str(ray_dir / "__init__.py"),
                    submodule_search_locations=[str(ray_dir)],
                )
            if name.startswith("ray.thirdparty_files.psutil"):
                raise AssertionError("lightweight probe should not inspect ray submodule specs")
            return None

        with patch("data_converter.any4_health.importlib.util.find_spec", side_effect=fake_find_spec):
            result = any4_health._probe_psutil_runtime(lightweight=True)

        self.assertEqual(result, "ok")

    def test_frozen_psutil_probe_tolerates_missing_ray_package(self) -> None:
        imported: list[str] = []
        real_import_module = any4_health.importlib.import_module

        def fake_import_module(name: str):
            if name.startswith("data_converter"):
                return real_import_module(name)
            imported.append(name)
            if name in {"psutil", "psutil._psutil_linux"}:
                return object()
            raise AssertionError(f"unexpected import: {name}")

        def fake_find_spec(name: str):
            if name == "ray.thirdparty_files.psutil":
                raise ModuleNotFoundError("No module named 'ray'")
            if name == "ray.thirdparty_files.psutil._psutil_linux":
                raise AssertionError("frozen probe should not inspect ray psutil private module when ray is absent")
            return None

        with (
            patch("data_converter.any4_health.sys.platform", "linux"),
            patch("data_converter.any4_health.importlib.import_module", side_effect=fake_import_module),
            patch("data_converter.any4_health.importlib.util.find_spec", side_effect=fake_find_spec),
        ):
            result = any4_health._probe_psutil_runtime(lightweight=False)

        self.assertEqual(result, "ok")
        self.assertEqual(imported, ["psutil", "psutil._psutil_linux"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


