from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from data_converter import main as main_module


class MainEntryTests(unittest.TestCase):
    def test_padding_helpers_support_class_based_flet_padding_api(self) -> None:
        class FakePadding:
            @classmethod
            def symmetric(cls, *, horizontal: int, vertical: int) -> tuple[str, int, int]:
                return ("symmetric", horizontal, vertical)

            @classmethod
            def only(
                cls, *, left: int = 0, right: int = 0, top: int = 0, bottom: int = 0
            ) -> tuple[str, int, int, int, int]:
                return ("only", left, right, top, bottom)

        fake_flet = SimpleNamespace(padding=SimpleNamespace(), Padding=FakePadding)

        with patch.object(main_module, "ft", fake_flet):
            self.assertEqual(main_module._padding_symmetric(horizontal=12, vertical=10), ("symmetric", 12, 10))
            self.assertEqual(
                main_module._padding_only(left=1, right=2, top=3, bottom=4),
                ("only", 1, 2, 3, 4),
            )

    def test_border_helpers_support_class_based_flet_border_api(self) -> None:
        class FakeBorder:
            @classmethod
            def all(cls, width: int, color: str) -> tuple[str, int, str]:
                return ("all", width, color)

            @classmethod
            def only(cls, *, bottom: object = None) -> tuple[str, object]:
                return ("only", bottom)

        fake_flet = SimpleNamespace(border=SimpleNamespace(), Border=FakeBorder)

        with patch.object(main_module, "ft", fake_flet):
            self.assertEqual(main_module._border_all(1, "#fff"), ("all", 1, "#fff"))
            self.assertEqual(main_module._border_only(bottom="edge"), ("only", "edge"))

    def test_entry_calls_freeze_support_before_worker_shard_dispatch(self) -> None:
        seen: list[object] = []

        def fake_freeze_support() -> None:
            seen.append("freeze_support")

        def fake_worker_shard(argv: list[str]) -> int:
            seen.append(("worker_shard", list(argv)))
            return 0

        with (
            patch("data_converter.main.multiprocessing.freeze_support", side_effect=fake_freeze_support),
            patch("data_converter.main.run_worker_shard_cli", side_effect=fake_worker_shard),
        ):
            exit_code = main_module.run_cli_entry(["--internal-run-worker-shard", "--payload-path", "payload.json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen[0], "freeze_support")
        self.assertEqual(seen[1], ("worker_shard", ["--payload-path", "payload.json"]))

    def test_entry_calls_freeze_support_before_launching_gui(self) -> None:
        seen: list[str] = []

        class FakeFlet:
            def app(self, *, target) -> None:
                del target
                seen.append("app")

        def fake_freeze_support() -> None:
            seen.append("freeze_support")

        def fake_ensure_flet() -> None:
            main_module.ft = FakeFlet()
            seen.append("ensure_flet")

        with (
            patch("data_converter.main.multiprocessing.freeze_support", side_effect=fake_freeze_support),
            patch("data_converter.main._ensure_flet", side_effect=fake_ensure_flet),
        ):
            exit_code = main_module.run_cli_entry([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen, ["freeze_support", "ensure_flet", "app"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
