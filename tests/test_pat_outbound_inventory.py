"""Static defense: every PAT outbound call goes through a verified contract."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"


class PatOutboundInventoryTests(unittest.TestCase):
    def test_only_coordinator_calls_thinq_connect_control_api(self) -> None:
        callers: list[str] = []
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "async_post_device_control"
                ):
                    callers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(
            callers,
            ["custom_components/my_lg/coordinator.py"],
        )

    def test_all_pat_entity_calls_build_or_pass_explicit_contract(self) -> None:
        allowed_files = {
            "button.py",
            "climate.py",
            "fan.py",
            "humidifier.py",
            "number.py",
            "select.py",
            "switch.py",
            "time.py",
        }
        calls: list[tuple[str, int, str]] = []
        for filename in sorted(allowed_files):
            path = PACKAGE / filename
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Await)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "async_control"
                ):
                    continue
                call = node.value
                # WideQ has device_id then ctrl_key; PAT has exactly one
                # positional contract argument and no kwargs.
                if len(call.args) != 1 or call.keywords:
                    continue
                calls.append(
                    (
                        filename,
                        node.lineno,
                        ast.unparse(call.args[0]),
                    )
                )

        self.assertTrue(calls)
        for filename, lineno, argument in calls:
            self.assertTrue(
                argument.startswith("build_pat_control_request(")
                or argument in {"request", "lambda state: build_cooktop_pat_request(state, self._location, power_level=target)"}
                or argument.startswith("lambda state: build_cooktop_pat_request("),
                f"uncontracted PAT call at {filename}:{lineno}: {argument}",
            )

    def test_write_only_operation_entities_remain_fail_closed(self) -> None:
        from custom_components.my_lg.button import (
            STYLER_BUTTONS,
            WASHTOWER_BUTTONS,
        )

        descriptions = (*WASHTOWER_BUTTONS, *STYLER_BUTTONS)
        self.assertTrue(descriptions)
        self.assertTrue(
            all(item.verified_request is None for item in descriptions)
        )


if __name__ == "__main__":
    unittest.main()
