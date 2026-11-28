"""An opencode log candidate under a home the process cannot traverse is absent, not fatal.

Self-hosted CI runs as `gha-runner`; `/home/alexey` is mode 750, so `Path.is_file()` on the
legacy hard-coded candidate raised PermissionError and failed every dispatch test.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agent_hub_host import launch_diagnostics as ld  # noqa: E402


class UnreadableCandidateTests(unittest.TestCase):
    def test_permission_error_candidate_is_skipped(self):
        real = Path.is_file

        def is_file(self):
            if str(self).startswith("/home/alexey/"):
                raise PermissionError(13, "Permission denied", str(self))
            return real(self)

        with mock.patch.object(Path, "is_file", is_file):
            self.assertNotIn(
                "/home/alexey/git/agent-hub/.local/platform-runtime/opencode-data/opencode/log/opencode.log",
                [str(p) for p in ld.get_opencode_log_paths()])
            self.assertIsInstance(ld.capture_log_offsets("opencode"), dict)

    def test_other_oserrors_still_raise(self):
        with mock.patch.object(Path, "is_file", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                ld.get_opencode_log_paths()


if __name__ == "__main__":
    unittest.main()
