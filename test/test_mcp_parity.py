"""Ensure the parity inventory follows the real CLI parser as commands evolve."""
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class MCPCLIParity(unittest.TestCase):
    def test_inventory_matches_current_cli(self):
        spec = importlib.util.spec_from_file_location("hub_mcp_inventory", ROOT / "scripts/mcp-cli-inventory.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.commands(), json.loads((ROOT / "test/mcp-cli-commands.json").read_text()))

    def test_phase2_omissions_are_current_cli_leaves_with_explicit_reasons(self):
        spec = importlib.util.spec_from_file_location("hub_mcp_inventory", ROOT / "scripts/mcp-cli-inventory.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = (ROOT / "src/mcp-curation.ts").read_text()
        match = re.search(r"const PHASE2_CLI_EXCLUSIONS: Record<string, string> = \{(.*?)\n\};", source, re.S)
        self.assertIsNotNone(match)
        keys = {
            quoted or bare
            for quoted, bare in re.findall(r"^\s*(?:'([^']+)'|([A-Za-z_][A-Za-z_0-9]*)):\s*'", match.group(1), re.M)
        }
        required = {
            "heartbeat", "sessions observe", "task-result", "start-agent", "start-session",
            "plan-agent", "submit", "jobs", "job", "artifact",
        }
        self.assertTrue(required <= keys)
        self.assertTrue(keys <= set(module.commands()))
