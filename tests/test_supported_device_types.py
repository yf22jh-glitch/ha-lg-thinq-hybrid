"""Every modelled ThinQ device type must be one this integration actually sets up.

The wireless stick cleaner was missing from both lists, so PAT returned it and
`async_setup_entry` silently skipped it: no coordinator, no Home Assistant
device, and no entry in the PAT-to-WideQ identity map the local bridge pairs
against. Nothing failed loudly - the appliance simply did not exist here.

`const.py` imports Home Assistant, and this invariant is pure literals, so the
constants are read with `ast` to keep the check runnable without that install.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


CONST_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "my_lg" / "const.py"


def _parse() -> tuple[dict[str, str], set[str]]:
    tree = ast.parse(CONST_PATH.read_text(encoding="utf8"))
    declared: dict[str, str] = {}
    whitelist: set[str] = set()
    for node in tree.body:
        targets = getattr(node, "targets", None) or ([node.target] if isinstance(node, ast.AnnAssign) else [])
        name = targets[0].id if targets and isinstance(targets[0], ast.Name) else None
        value = getattr(node, "value", None)
        if name is None or value is None:
            continue
        if name.startswith("DEVICE_TYPE_") and isinstance(value, ast.Constant) and isinstance(value.value, str):
            declared[name] = value.value
        elif name == "SUPPORTED_DEVICE_TYPES" and isinstance(value, ast.Set):
            whitelist = {element.id for element in value.elts if isinstance(element, ast.Name)}
    return declared, whitelist


class SupportedDeviceTypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declared, self.whitelist = _parse()
        self.assertTrue(self.declared, "no DEVICE_TYPE_* constants were parsed")
        self.assertTrue(self.whitelist, "SUPPORTED_DEVICE_TYPES was not parsed as a set of names")

    def test_every_declared_device_type_is_set_up(self) -> None:
        self.assertEqual(
            sorted(set(self.declared) - self.whitelist),
            [],
            "declared device types absent from SUPPORTED_DEVICE_TYPES",
        )

    def test_whitelist_holds_only_declared_device_types(self) -> None:
        self.assertEqual(sorted(self.whitelist - set(self.declared)), [])

    def test_stick_cleaner_is_covered(self) -> None:
        self.assertEqual(self.declared.get("DEVICE_TYPE_STICK_CLEANER"), "DEVICE_STICK_CLEANER")
        self.assertIn("DEVICE_TYPE_STICK_CLEANER", self.whitelist)


if __name__ == "__main__":
    unittest.main()
