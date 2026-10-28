import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "host"))

from agent_hub_host import config


class TestDomainConfig(unittest.TestCase):
    def test_default_base_url_is_custom_domain(self):
        self.assertEqual(config.DEFAULT_BASE_URL, "https://hub.dtcdev.click")

    def test_canonical_origin_custom_domain(self):
        origin = config.canonical_origin("https://hub.dtcdev.click")
        self.assertEqual(origin, ("https", "hub.dtcdev.click", 443))

        origin_with_path = config.canonical_origin("https://hub.dtcdev.click/api/device/code")
        self.assertEqual(origin_with_path, ("https", "hub.dtcdev.click", 443))

    def test_same_origin_rules(self):
        self.assertTrue(config.same_origin("https://hub.dtcdev.click", "https://hub.dtcdev.click/auth/login"))
        self.assertTrue(config.same_origin("https://hub.dtcdev.click/", "https://hub.dtcdev.click"))
        # Distinct origins
        self.assertFalse(config.same_origin("https://hub.dtcdev.click", "https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev"))

    def test_install_script_default_base(self):
        install_sh = REPO_ROOT / "public" / "cli" / "install.sh"
        self.assertTrue(install_sh.is_file(), "install.sh must exist")
        content = install_sh.read_text(encoding="utf-8")
        self.assertIn('BASE_DEFAULT="https://hub.dtcdev.click"', content)
        self.assertIn('https://hub.dtcdev.click', content)

    def test_manifest_default_base_and_assets(self):
        manifest_path = REPO_ROOT / "public" / "cli" / "manifest.json"
        self.assertTrue(manifest_path.is_file(), "manifest.json must exist")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("default_base_url"), "https://hub.dtcdev.click")
        self.assertIn("install.sh", manifest.get("files", []))
        self.assertIn("hub.pyz", manifest.get("files", []))
        self.assertIsInstance(manifest.get("built_at_ms"), int)
        self.assertGreater(manifest.get("built_at_ms"), 0)

    def test_build_hub_cli_default_base(self):
        build_script = REPO_ROOT / "scripts" / "build-hub-cli.py"
        content = build_script.read_text(encoding="utf-8")
        self.assertIn('"default_base_url": "https://hub.dtcdev.click"', content)
        # The deployed Worker reads built_at_ms from its own manifest to
        # label stale placement-reject warnings (intake 1bc3add1 AC2).
        self.assertIn('"built_at_ms": built_at_ms', content)


if __name__ == "__main__":
    unittest.main()
