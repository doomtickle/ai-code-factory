"""The tool registry, and broker/tools.txt kept in sync with it."""

import unittest
from pathlib import Path

from agentbroker import tools  # noqa: F401 - registers every tool
from agentbroker.core import registry


class RegistryTests(unittest.TestCase):
    def test_tools_txt_matches_the_registry(self):
        listed = [line.strip() for line in (Path(__file__).parents[1] / "tools.txt").read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        self.assertEqual(listed, list(registry.TOOLS),
                         "broker/tools.txt must list every registered tool, in registration order")

    def test_duplicate_registration_is_refused(self):
        name = next(iter(registry.TOOLS))
        with self.assertRaises(RuntimeError):
            registry.register({name: registry.TOOLS[name]})

    def test_tool_list_schema(self):
        for entry in registry.tool_list():
            self.assertEqual(set(entry), {"name", "description", "inputSchema"})
            schema = entry["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertTrue(set(schema["required"]) <= set(schema["properties"]), entry["name"])


if __name__ == "__main__":
    unittest.main()
