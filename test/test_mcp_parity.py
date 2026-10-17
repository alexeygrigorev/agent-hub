"""Ensure the parity inventory follows the real CLI parser as commands evolve."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class MCPCLIParity(unittest.TestCase):
    def test_inventory_matches_current_cli(self):
        spec = importlib.util.spec_from_file_location("hub_mcp_inventory", ROOT / "scripts/mcp-cli-inventory.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.commands(), json.loads((ROOT / "test/mcp-cli-commands.json").read_text()))
