from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from data_converter import any4_health


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
