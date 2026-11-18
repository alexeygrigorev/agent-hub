"""Offline tests for the maintained quota-dispatcher archive builder and entry."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agent_hub_host.quota_dispatch_manager import StableRuntime

REPO_ROOT = Path(__file__).resolve().parents[3]


class QuotaDispatchEntryTests(unittest.TestCase):
    def _short_temp(self):
        d = tempfile.TemporaryDirectory(prefix="qde-", dir=os.environ.get("RUNNER_TEMP") or None)
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def test_builder_deterministic_closure_and_isolated_system_python_entry(self):
        spec = importlib.util.spec_from_file_location(
            "build_quota_dispatcher", REPO_ROOT / "scripts" / "build-quota-dispatcher.py"
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        root = self._short_temp()
        out1 = root / "disp1.pyz"
        out2 = root / "disp2.pyz"
        manifest1 = root / "manifest1.json"
        r1 = builder.build_archive(out1, manifest_out=manifest1)
        r2 = builder.build_archive(out2)

        self.assertEqual(r1["sha256"], r2["sha256"])
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        self.assertEqual(json.loads(manifest1.read_text(encoding="utf-8"))["sha256"], r1["sha256"])

        with zipfile.ZipFile(out1, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("__main__.py", names)
            self.assertIn("MANIFEST.json", names)
            self.assertIn("agent_hub_host/quota_dispatch_entry.py", names)
            self.assertNotIn("agent_hub_host/agent.py", names)
            manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8"))
            self.assertEqual(manifest["entry"], "agent_hub_host.quota_dispatch_entry:main")
            for rel in builder.SOURCE_CLOSURE:
                expected = hashlib.sha256((REPO_ROOT / "hub2" / "host" / "agent_hub_host" / rel).read_bytes()).hexdigest()
                self.assertEqual(manifest["sources"][f"agent_hub_host/{rel}"], expected)

        cfg = root / "v2.json"
        cfg.write_text('{"server":"http://127.0.0.1:9","token":"t","machine_id":"m","roots":[],"engines":[]}')
        fd = os.open(str(out1), os.O_RDONLY)
        self.addCleanup(lambda: os.close(fd))
        help_res = subprocess.run(
            [sys.executable, "-I", f"/proc/self/fd/{fd}", "--help"],
            pass_fds=(fd,), capture_output=True, text=True, check=False,
        )
        self.assertEqual(help_res.returncode, 0, help_res.stderr)
        self.assertIn("quota-dispatch", help_res.stdout)

        run_res = subprocess.run(
            [sys.executable, "-I", f"/proc/self/fd/{fd}", "--config", str(cfg), "quota-dispatch"],
            pass_fds=(fd,), capture_output=True, text=True, check=False,
        )
        self.assertEqual(run_res.returncode, 0, run_res.stderr)
        payload = json.loads(run_res.stdout)
        self.assertEqual(payload, {"enabled": False, "dispatched": False, "authority": "NONE", "complete": False})

        # Also verify primary-prelude subcommand in the built archive runs offline via python3 -I /proc/self/fd/<fd>
        src_dir = root / "src"
        src_dir.mkdir(mode=0o700)
        q_file = src_dir / "hub-quota-publish.py"
        q_file.write_bytes(b"import sys\ndef main(argv=None, *, _managed_observer=None):\n    return 37 if argv == ['--check', '--health-timeout', '8'] and _managed_observer is None else 99\n")
        q_file.chmod(0o444)
        c_file = src_dir / "hub_alert_client.py"
        c_file.write_bytes(b"pass\n")
        c_file.chmod(0o444)
        q_sha = hashlib.sha256(q_file.read_bytes()).hexdigest()
        c_sha = hashlib.sha256(c_file.read_bytes()).hexdigest()
        prelude_res = subprocess.run(
            [sys.executable, "-I", f"/proc/self/fd/{fd}", "primary-prelude",
             "0", q_sha, c_sha, str(root), str(q_file), str(c_file), "--", "--check", "--health-timeout", "8"],
            pass_fds=(fd,), capture_output=True, text=True, check=False,
        )
        self.assertEqual(prelude_res.returncode, 37, prelude_res.stderr)

    def test_user_manager_env_offline_validation(self):
        root = self._short_temp()
        rt = root / "rt"
        rt.mkdir(mode=0o700)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(rt / "bus"))
        self.addCleanup(sock.close)
        real_open = os.open
        uid = os.getuid()
        with patch("os.open", side_effect=lambda p, *a, **kw: real_open(str(rt), *a, **kw) if p == f"/run/user/{uid}" else real_open(p, *a, **kw)):
            env = StableRuntime.user_manager_env(uid)
        self.assertEqual(env["XDG_RUNTIME_DIR"], f"/run/user/{uid}")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path=/run/user/{uid}/bus")
