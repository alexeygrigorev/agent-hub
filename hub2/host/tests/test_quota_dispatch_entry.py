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
import time
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

    def test_managed_observer_actual_client_abi_contract_and_failed_emit(self):
        from agent_hub_host.quota_dispatch_entry import _ManagedObserver
        spec = importlib.util.spec_from_file_location("actual_client_abi", REPO_ROOT / "scripts" / "hub_alert_client.py")
        client_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client_mod)

        seen = []
        def _ok_exchange(_path, req, *, deadline=None):
            seen.append(req["frame"])
            return {"seq": req["frame"]["seq"], "ok": True}

        with patch.object(client_mod, "exchange", _ok_exchange):
            client = client_mod.Client(None, "a" * 32, None, "quota_monitor")
            obs = _ManagedObserver(client)
            self.assertTrue(obs.emit("begin"))
            self.assertTrue(obs.emit("decision", "observation", "quota_fresh", {"no_mail": True}))
            self.assertTrue(obs.emit("terminal", flags={"exit_success": True}))
            self.assertFalse(client.dirty)
            self.assertEqual([f["phase"] for f in seen], ["begin", "decision", "terminal"])

        with patch.object(client_mod, "exchange", side_effect=OSError("wire_down")):
            bad_client = client_mod.Client(None, "b" * 32, None, "quota_monitor")
            bad_obs = _ManagedObserver(bad_client)
            self.assertFalse(bad_obs.emit("begin"))
            self.assertTrue(bad_client.dirty)

    def test_built_archive_primary_prelude_qualified_joined_lifecycle(self):
        import types
        from agent_hub_host import alert_authority as ram, alert_broker as wire
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, FanotifyReader, OrderedBarrier, SourceGate, hold_source_fd, prepare_selected_quota,
        )

        spec = importlib.util.spec_from_file_location(
            "build_quota_dispatcher_q", REPO_ROOT / "scripts" / "build-quota-dispatcher.py"
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        root = self._short_temp()
        pyz = root / "dispatcher.pyz"
        builder.build_archive(pyz)
        pyz_fd = os.open(str(pyz), os.O_RDONLY)
        self.addCleanup(lambda: os.close(pyz_fd))

        broker_dir = root / "broker"
        gate_dir = root / "gate"
        src_dir = root / "src"
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)

        primary_code = (
            "import json, os\n"
            "def main(argv=None, *, _managed_observer=None):\n"
            "    assert _managed_observer is not None\n"
            "    assert _managed_observer.emit('begin') is True\n"
            "    assert _managed_observer.emit('decision', 'observation', 'quota_fresh', {'no_mail': True}) is True\n"
            "    assert _managed_observer.emit('terminal', flags={'exit_success': True}) is True\n"
            "    assert _managed_observer.client.dirty is False\n"
            "    return 0 if argv == ['--check', '--health-timeout', '8'] else 99\n"
        ).encode("utf-8")
        q_file = src_dir / "hub-quota-publish.py"
        q_file.write_bytes(primary_code)
        q_file.chmod(0o444)
        shutil.copyfile(REPO_ROOT / "scripts" / "hub_alert_client.py", src_dir / "hub_alert_client.py")
        (src_dir / "hub_alert_client.py").chmod(0o444)
        shutil.copyfile(REPO_ROOT / "scripts" / "hub-alert-quota-dispatch.py", src_dir / "hub-alert-quota-dispatch.py")
        (src_dir / "hub-alert-quota-dispatch.py").chmod(0o444)

        quota_fd = hold_source_fd(src_dir, "hub-quota-publish.py")
        client_fd = hold_source_fd(src_dir, "hub_alert_client.py")
        prelude_fd = hold_source_fd(src_dir, "hub-alert-quota-dispatch.py")
        gate = SourceGate(gate_dir)
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        os.close(w_fan)

        unit_name = "agent-hub-quota-monitor.service"
        cgroup = "/user.slice/user-1000.slice/user@1000.service/app.slice/agent-hub-quota-monitor.service"
        primary_args = ["--check", "--health-timeout", "8"]
        exec_argv = lambda dl_ns: [
            sys.executable, "-I", f"/proc/self/fd/{pyz_fd}", "primary-prelude",
            str(dl_ns), quota_fd.pin.sha256, client_fd.pin.sha256,
            str(gate_dir), str(q_file), str(src_dir / "hub_alert_client.py"), "--", *primary_args,
        ]
        child_proc = {}
        events_cb = {}
        drain_count = {"n": 0}
        class _FakeFan(FanotifyReader):
            def drain(self, group, obs):
                drain_count["n"] += 1
                obs.unknown = False
                if drain_count["n"] == 1:
                    obs.records = []
                else:
                    obs.records = [{"pid": child_proc["p"].pid, "dev": quota_fd.pin.dev, "ino": quota_fd.pin.ino, "mask": 0x20}]

        def _fake_runner_run(argv, timeout):
            if "--property=ExecStart" in argv:
                return 0, ("ExecStart=" + " ".join(exec_argv(child_proc["dl_ns"]))).encode("utf-8")
            return 0, b"ExecMainCode=1\nExecMainStatus=0\n"

        poll_n = {"n": 0}
        def _fake_poll(self_b):
            poll_n["n"] += 1
            if poll_n["n"] >= 2:
                if not any(e.get("member") == "JobRemoved" for e in events_cb["list"]):
                    events_cb["cb"]({"member": "JobRemoved", "job_id": 1, "unit": unit_name, "result": "done", "stream_index": 5, "manager_serial": 11, "epoch": "e" * 32})
                return {"stream_index": 6}
            return {"stream_index": 2}

        def _fake_collector(_self, cb):
            events_cb["cb"] = lambda ev: (events_cb["list"].append(ev), cb(ev))[1]
            events_cb["list"] = []
            return types.SimpleNamespace(
                wire=types.SimpleNamespace(receive=lambda *a: False), epoch="e" * 32,
                fault=None, started=True, pending=None, jobs={},
                start=lambda: (events_cb["cb"]({"member": "JobNew", "job_id": 1, "unit": unit_name, "result": None, "stream_index": 3, "manager_serial": 10, "epoch": "e" * 32}), {"stream_index": 1})[1],
                handle=lambda *a: None, barrier=lambda: {"stream_index": 2}, close=lambda: None,
            )

        boot = "a" * 8 + "-" + "a" * 4 + "-" + "a" * 4 + "-" + "a" * 4 + "-" + "a" * 12
        prepared = prepare_selected_quota(
            source=quota_fd, companions=(client_fd, prelude_fd), gate=gate, group=r_fan,
            plan={"units": {"healthy": unit_name}, "cgroups": {"healthy": cgroup}},
            exec_argv=exec_argv, service_deadline=time.monotonic() + 10.0,
            fan=_FakeFan(), observation=FanotifyObservation(),
            runner=types.SimpleNamespace(run=_fake_runner_run),
            manager_environment=types.SimpleNamespace(
                show=lambda r, p, k, d: {"Id": unit_name, "MainPID": str(child_proc["p"].pid) if child_proc["p"].poll() is None else "0", "ControlGroup": cgroup, "InvocationID": "b" * 32},
                owned=lambda s, p, k, inv=None: "b" * 32,
                remaining=lambda d, cap: 1.0,
            ),
            stable=types.SimpleNamespace(
                UID=os.getuid(),
                source_stamp=lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
                checked_source=lambda *a: None,
                inspect_exec=lambda raw, exp: None,
                inspect_process=lambda pid, cg: (pid, 456, boot),
                alive=lambda fd: child_proc["p"].poll() is None,
            ),
            retained_source_fd=quota_fd.fd,
        )
        def _launch(ticket):
            self.assertIsNotNone(ticket)
            child_proc["dl_ns"] = int(ticket.deadline * 1e9)
            child_proc["p"] = subprocess.Popen(exec_argv(child_proc["dl_ns"]), pass_fds=(pyz_fd,))
            return 0

        import select as _sel
        from agent_hub_host import quota_dispatch as _qd
        real_mono = time.monotonic
        real_select = _sel.select
        real_reserve = _qd.QuotaDispatch.reserve
        def _fast_select(r, w, x, timeout=None):
            return real_select(r, w, x, min(timeout, 0.002) if timeout is not None else None)
        def _reserve_with_fork_slack(self_owner, source, deadline):
            ticket = real_reserve(self_owner, source, deadline)
            if ticket is not None:
                ticket.deadline = real_mono() + 2.0
            return ticket
        with patch.object(wire.Broker, "_collector", _fake_collector), \
             patch("select.select", _fast_select), \
             patch.object(_qd.QuotaDispatch, "reserve", _reserve_with_fork_slack), \
             patch("agent_hub_host.quota_dispatch_manager.OrderedBarrier.begin", lambda self, d: None), \
             patch("agent_hub_host.quota_dispatch_manager.OrderedBarrier.poll", _fake_poll), \
             patch("os.pidfd_open", return_value=os.dup(r_fan)):
            res = run_same_process_quota(
                prepared, _launch, enabled=True, broker_directory=broker_dir,
                identity=lambda pid: ram.ProcessKey(pid, 456, boot),
            )
        rc = child_proc["p"].wait(timeout=5.0)
        self.assertEqual(rc, 0)
        self.assertTrue(res["dispatched"])
        self.assertTrue(res["reserved"])
        self.assertEqual(res["phase"], "done")
        self.assertFalse(res["complete"])
