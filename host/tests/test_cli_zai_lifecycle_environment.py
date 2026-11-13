"""Pure managed-env composition tests; no profiles, credentials or processes."""
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import provisioning as provisioning

TEMP_BASE = Path(__file__).resolve().parents[2] / ".local/tmp"

FILTER = "error,codex_tool_lifecycle=info"

class ZaiLifecycleEnvironmentTests(unittest.TestCase):
    def compose(self, override=None):
        TEMP_BASE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_BASE) as temporary:
            home = Path(temporary)/"home"
            bundle = {"provider":"zai", "env":{} if override is None else {"RUST_LOG":override}}
            with patch.dict(os.environ, {"RUST_LOG":FILTER, "SYNTHETIC_ACCOUNT_CONTEXT":"preserved"}, clear=True), \
                 patch.object(provisioning.shutil, "which", return_value="/synthetic/node"), \
                 patch.object(provisioning, "locate_zcode_runtime", return_value=Path(temporary)/"runtime.cjs"), \
                 patch.object(provisioning, "shared_dataops_dir", return_value=None), \
                 patch.object(provisioning, "load_git_identity", return_value={}):
                env = provisioning.engine_environment(bundle, home, Path(temporary))
            self.assertEqual(env["HOME"], str(home))
            self.assertEqual(env["CODEX_HOME"], str(home/".zcodex"))
            self.assertEqual(env["ZCODE_HOME"], str(home/".zcode"))
            self.assertEqual(env["SYNTHETIC_ACCOUNT_CONTEXT"], "preserved")
            self.assertEqual(env["ZCODE_CJS"], str(Path(temporary)/"runtime.cjs"))
            self.assertEqual(env["ZCODE_NODE"], "/synthetic/node")
            return env

    def test_actual_provisioning_preserves_inherited_fixed_child_filter(self):
        self.assertEqual(self.compose()["RUST_LOG"], FILTER)

    def test_existing_explicit_bundle_filter_retains_precedence(self):
        self.assertEqual(self.compose("error,synthetic_operator=warn")["RUST_LOG"], "error,synthetic_operator=warn")

    def test_existing_explicit_empty_bundle_value_remains_unchanged(self):
        self.assertEqual(self.compose("")["RUST_LOG"], "")

if __name__ == "__main__":
    unittest.main()
