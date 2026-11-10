import os
import subprocess
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch


BOOTSTRAP_PATH = Path(__file__).parents[1] / "bootstrap.py"
BOOTSTRAP_SPEC = spec_from_file_location("hub2_host_bootstrap", BOOTSTRAP_PATH)
bootstrap = module_from_spec(BOOTSTRAP_SPEC)
BOOTSTRAP_SPEC.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def run_bootstrap(self, uv_path):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = {"LOCALAPPDATA": temp.name}
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        exists = patch.object(Path, "exists", return_value=True)
        exists.start()
        self.addCleanup(exists.stop)
        with patch.dict(os.environ, env), \
             patch.object(bootstrap.shutil, "which", return_value=uv_path), \
             patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            self.assertEqual(bootstrap.main(), 0)
        return calls

    def test_uv_reinstalls_current_local_host_package(self):
        calls = self.run_bootstrap("/usr/bin/uv")
        install = calls[1]
        self.assertEqual(install[:3], ["/usr/bin/uv", "pip", "install"])
        self.assertIn("--reinstall-package", install)
        self.assertEqual(install[install.index("--reinstall-package") + 1], bootstrap.PACKAGE_NAME)
        self.assertEqual(Path(install[-1]).resolve(), Path(bootstrap.__file__).parent.resolve())

    def test_pip_fallback_forces_reinstall_of_current_local_host_package(self):
        calls = self.run_bootstrap(None)
        install = calls[0]
        self.assertIn("--force-reinstall", install)
        self.assertEqual(Path(install[-1]).resolve(), Path(bootstrap.__file__).parent.resolve())


if __name__ == "__main__":
    unittest.main()
