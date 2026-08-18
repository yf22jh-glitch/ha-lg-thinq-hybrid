"""Static defense: WideQ entities cannot bypass verified outbound routing."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "my_lg"


def _attribute_calls(path: Path, names: set[str]) -> list[ast.Call]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    ]


class WideqOutboundInventoryTests(unittest.TestCase):
    def test_only_wideq_coordinator_calls_integration_client_control(self) -> None:
        callers: list[str] = []
        for path in PACKAGE.glob("*.py"):
            for call in _attribute_calls(path, {"async_control"}):
                owner = call.func.value
                if isinstance(owner, ast.Attribute) and owner.attr == "client":
                    callers.append(path.name)

        self.assertEqual(callers, ["coordinator_wideq.py"])

    def test_general_wideq_entities_use_only_common_verified_helper(self) -> None:
        helper_calls = 0
        direct_calls: list[tuple[str, str, int]] = []
        for filename in ("switch.py", "select.py", "number.py", "text.py"):
            path = PACKAGE / filename
            tree = ast.parse(path.read_text(), filename=str(path))
            for class_node in (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef)
                and node.name.startswith("MyLgWideq")
            ):
                for call in (
                    node
                    for node in ast.walk(class_node)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                ):
                    if call.func.attr == "_wideq_set":
                        helper_calls += 1
                    elif call.func.attr in {
                        "async_control",
                        "async_control_and_verify",
                    }:
                        direct_calls.append(
                            (filename, class_node.name, call.lineno)
                        )

        self.assertGreater(helper_calls, 0)
        self.assertEqual(direct_calls, [])

    def test_verified_coordinator_entrypoints_are_narrowly_scoped(self) -> None:
        callers: list[str] = []
        for path in PACKAGE.glob("*.py"):
            calls = _attribute_calls(path, {"async_control_and_verify"})
            callers.extend(path.name for _call in calls)

        self.assertEqual(sorted(callers), ["button.py", "entity.py", "services.py"])

    def test_entities_never_apply_wideq_optimistic_state(self) -> None:
        callers: list[tuple[str, int]] = []
        for path in PACKAGE.glob("*.py"):
            for call in _attribute_calls(
                path,
                {"apply_optimistic", "apply_power_save_optimistic"},
            ):
                callers.append((path.name, call.lineno))

        self.assertEqual(callers, [])


if __name__ == "__main__":
    unittest.main()
