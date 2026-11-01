"""Host quota probes never run a probe that can launch engine login (intake 5814e109 AC e/A3)."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import quota_snapshot as qs


class GuardNativeQuseArgv(unittest.TestCase):
    def test_default_quse_excludes_gemini(self):
        argv = qs.guard_native_quse_argv(["quse", "--json"])
        self.assertEqual(argv[:2], ["quse", "--json"])
        self.assertNotIn("gemini", argv)
        self.assertIn("claude", argv)
        self.assertIn("codex", argv)

    def test_windows_paths_are_recognised(self):
        for exe in (r"C:\hub\venv\Scripts\quse.EXE", "/opt/venv/bin/quse"):
            argv = qs.guard_native_quse_argv([exe, "--json"])
            self.assertNotIn("gemini", argv, exe)
            self.assertGreater(len(argv), 2, exe)

    def test_module_invocation_is_guarded(self):
        argv = qs.guard_native_quse_argv(["python.exe", "-m", "quse.cli", "--json"])
        self.assertNotIn("gemini", argv)
        self.assertGreater(len(argv), 4)

    def test_non_quse_adapter_untouched(self):
        argv = ["hub", "engines", "quota"]
        self.assertEqual(qs.guard_native_quse_argv(argv), argv)

    def test_explicit_providers_untouched(self):
        argv = ["quse", "--json", "claude"]
        self.assertEqual(qs.guard_native_quse_argv(argv), argv)

    def test_opt_in_env_restores_full_probe(self):
        with patch.dict(os.environ, {"AGENT_HUB_HOST_PROBE_INTERACTIVE_AUTH": "1"}):
            self.assertEqual(qs.guard_native_quse_argv(["quse", "--json"]), ["quse", "--json"])

    def test_subprocess_receives_guarded_argv(self):
        seen = {}

        class Boom(Exception):
            pass

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            raise Boom()

        with patch.object(qs.subprocess, "Popen", fake_popen):
            try:
                qs.execute_quse("quse")
            except Exception:
                pass
        self.assertNotIn("gemini", seen["argv"])
        self.assertIn("--json", seen["argv"])


if __name__ == "__main__":
    unittest.main()
