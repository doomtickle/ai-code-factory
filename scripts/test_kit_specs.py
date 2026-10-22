"""Tests for the managed settings the kit specs write into every sandbox.

Run:
  python3 -m unittest discover -s scripts -p 'test_kit_specs.py' -v
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = [ROOT / "kits" / "coordinator" / "spec.yaml"] + sorted(
    (ROOT / "roles").glob("*/kit/spec.yaml")
)
PRINTF = re.compile(r"printf '%s\\n' '(\{.*?\})'\s*>\s*/etc/claude-code/managed-settings\.json")


def managed_settings(spec: Path) -> dict:
    match = PRINTF.search(spec.read_text(encoding="utf-8"))
    if not match:
        raise AssertionError(f"{spec}: no managed-settings printf found")
    return json.loads(match.group(1))


class KitManagedSettings(unittest.TestCase):
    def test_specs_found(self):
        names = {p.parent.parent.name if p.parent.name == "kit" else p.parent.name for p in SPECS}
        self.assertIn("coordinator", names)
        self.assertGreaterEqual(len(SPECS), 4)

    def test_every_kit_disables_claude_ai_connectors(self):
        for spec in SPECS:
            with self.subTest(spec=str(spec.relative_to(ROOT))):
                settings = managed_settings(spec)
                self.assertIs(settings.get("disableClaudeAiConnectors"), True)

    def test_existing_keys_kept(self):
        coordinator = managed_settings(ROOT / "kits" / "coordinator" / "spec.yaml")
        self.assertEqual(coordinator["model"], "opus")
        self.assertIn("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", coordinator["env"])
        self.assertIn("Bash", coordinator["permissions"]["deny"])
        for role in ("developer", "reviewer", "researcher"):
            deny = managed_settings(ROOT / "roles" / role / "kit" / "spec.yaml")["permissions"]["deny"]
            self.assertIn("WebFetch", deny)


if __name__ == "__main__":
    unittest.main()
