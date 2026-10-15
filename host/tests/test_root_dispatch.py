"""Tests for the trusted Root dispatch adapter (docs/ROOT-DISPATCH.md).

Fixture-only positives are explicit: the injected launcher executor
(``FakeRootEnv.execute``) simulates the NOT YET ACCEPTED CAG script hook
protocol in a temp state dir with a real child process; every receipt it yields
is marked ``fixture_only`` and none of it is operational evidence. The negative
tests prove fail-closed behaviour of the host without any fake success.
"""

import contextlib
import fcntl
import http.server
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import root_dispatch as rd
from agent_hub_host.guarded_root import get_current_pid_namespace, get_pid_starttime_ticks
from agent_hub_host.root_dispatch import (
    DispatchError,
    HubSchedulingReader,
    RootDispatchAdapter,
    SchedulingCache,
    TrustedRootConfig,
    canonical_incumbent_status,
    read_canonical_state,
    validate_entrypoint,
)
from agent_hub_host.runtime import LauncherQualification

REAL_CAG_SCRIPT = os.path.expanduser("~/git/cloudflare-agent-git/scripts/root/headless-root-run.sh")


def jwrite(path, data, mode=0o644):
    path = pathlib.Path(path)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def iso_now(delta=0.0):
    return rd.iso_utc(time.time() + delta)


class FakeUnitController:
    """Fixture of the systemd user manager: exact-invocation signalling only."""

    def __init__(self):
        self.units = {}
        self.signals = []
        self.refused = []
        self.stopped = threading.Event()

    def activate(self, unit, inv):
        self.units[unit] = {"inv": inv, "state": "active"}

    def deactivate(self, unit):
        self.units.pop(unit, None)

    def show(self, unit):
        u = self.units.get(unit)
        if not u:
            return {"LoadState": "not-found", "ActiveState": "inactive", "InvocationID": "", "ControlGroup": ""}
        return {"LoadState": "loaded", "ActiveState": u["state"], "InvocationID": u["inv"],
                "ControlGroup": f"/user.slice/user-1000.slice/app.slice/{unit}"}

    def signal(self, unit, expected_invocation, verb):
        live = self.show(unit)["InvocationID"]
        if not expected_invocation or not live or expected_invocation != live:
            self.refused.append((unit, expected_invocation, verb))
            return False
        self.signals.append((unit, expected_invocation, verb))
        self.stopped.set()
        return True


class FakeRootEnv:
    """Fixture-only temp Root environment plus a script-protocol simulator.

    ``mode`` selects what the simulated script/unit does:
    ok, native, crash, noop, skip, quota_held, error, timeout, hang_before_receipt,
    hang_after_ack, foreign_receipt.
    """

    def __init__(self, test, mode="ok"):
        self.test = test
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        test.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.repo = self.base / "repo"
        script = self.repo / "scripts" / "root" / "headless-root-run.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)
        self.script = script
        self.state = self.base / "state" / "headless-root"
        (self.state / "runs").mkdir(parents=True)
        self.host_dir = self.base / "state" / "agent-hub-host"
        self.config = TrustedRootConfig.build(script, self.repo, self.state, self.host_dir)
        self.child = subprocess.Popen(["sleep", "300"])
        test.addCleanup(self._kill_child)
        self.controller = FakeUnitController()
        self.mode = mode
        self.requests = []
        self.sched = (True, "scheduling_enabled")
        self.quota = (True, "quota ok")
        self.invocation = "inv-0001"
        self.native_epoch = 7
        self.executions = 0

    def _kill_child(self):
        self.child.kill()
        self.child.wait()

    # -- seams ---------------------------------------------------------
    def proc_probe(self, pid):
        ident = rd.read_process_identity(pid)
        if ident is not None:
            # fixture: claim the unit's cgroup for the stand-in script process
            ident["cgroup"] = "0::/user.slice/app.slice/" + (self.requests[-1].job_env["HUB_LAUNCHER_UNIT"]
                                                               if self.requests else "")
        return ident

    def adapter(self, **kw):
        args = dict(
            config=self.config, launcher_executor=self.execute,
            scheduling_check=lambda *a: self.sched, quota_check=lambda: self.quota,
            quse_provider=lambda: {"fixture": "quse"}, unit_controller=self.controller,
            proc_probe=self.proc_probe, timeout_sec=20.0,
        )
        args.update(kw)
        return RootDispatchAdapter(**args)

    def adapter_kwargs(self):
        return dict(
            launcher_executor=self.execute, quota_check=lambda: self.quota,
            quse_provider=lambda: {"fixture": "quse"}, unit_controller=self.controller,
            proc_probe=self.proc_probe, timeout_sec=20.0,
        )

    def begin(self, job_id="job-1", epoch=4, **kw):
        return self.adapter(**kw).begin(job_id, "SECRET-JOB-TOKEN", epoch, "https://hub.example", "host-1")

    # -- simulated script ------------------------------------------------
    def state_record(self, env, **over):
        now = int(time.time())
        d = {
            "run_id": env["HUB_EXPECTED_RUN_ID"], "pid": self.child.pid, "pgid": self.child.pid,
            "pid_namespace": get_current_pid_namespace(), "pid_start_ticks": get_pid_starttime_ticks(self.child.pid),
            "epoch": self.native_epoch, "hub_job_id": env["HUB_JOB_ID"],
            "hub_lease_epoch": int(env["HUB_LEASE_EPOCH"]), "launcher_task_id": env["HUB_LAUNCHER_TASK_ID"],
            "launcher_unit": env["HUB_LAUNCHER_UNIT"], "launcher_invocation_id": self.invocation,
            "start": now, "heartbeat": now, "status": "running", "host": "fixture",
        }
        d.update(over)
        return d

    def receipt_record(self, env, kind="observed_root_bootstrap", **over):
        d = {
            "schema_version": 1, "kind": kind, "job_id": env["HUB_JOB_ID"], "host_id": env["HUB_HOST_ID"],
            "hub_lease_epoch": int(env["HUB_LEASE_EPOCH"]), "native_epoch": self.native_epoch,
            "run_id": env["HUB_EXPECTED_RUN_ID"], "launcher_task_id": env["HUB_LAUNCHER_TASK_ID"],
            "launcher_unit": env["HUB_LAUNCHER_UNIT"], "launcher_invocation_id": self.invocation,
            "observed_at_utc": iso_now(), "actual_action": "bootstrap: lock acquired, state written",
            "actual_action_digest": "a" * 64, "native_session_id": None,
            "source_entrypoint_sha256": rd.file_sha256(self.script),
        }
        if kind == "observed_native_tool":
            d.update(actual_action="tool: shell", native_session_id="codex-session-1")
        d.update(over)
        return d

    def write_summary(self, env, status="ok", exit_code=0):
        jwrite(self.state / "runs" / f"{env['HUB_EXPECTED_RUN_ID']}.json", {
            "run_id": env["HUB_EXPECTED_RUN_ID"], "epoch": self.native_epoch, "status": status,
            "exit_code": exit_code, "started": int(time.time()), "ended": int(time.time()),
        })

    def execute(self, req):
        self.requests.append(req)
        self.executions += 1
        env = req.job_env
        unit = env["HUB_LAUNCHER_UNIT"]
        self.controller.activate(unit, self.invocation)
        receipt = {"unit_name": unit, "exit_code": 0, "invocation_id": self.invocation, "cleanup_verified": True}
        sf = self.state / "state.json"
        first = pathlib.Path(env["HUB_FIRST_ACTION_FILE"])
        ack = pathlib.Path(env["HUB_START_ACK_FILE"])
        mode = self.mode
        try:
            if mode == "noop":
                time.sleep(0.3)
                return receipt
            if mode == "skip":
                self.write_summary(env, "skipped")
                return receipt
            if mode == "quota_held":
                self.write_summary(env, "quota_held", 75)
                return receipt
            if mode == "hang_before_receipt":
                self.controller.stopped.wait(60)
                receipt["exit_code"] = 143
                return receipt
            jwrite(sf, self.state_record(env))
            if mode == "foreign_receipt":
                jwrite(first, self.receipt_record(env, run_id="SOMEONE-ELSES-RUN"))
            else:
                jwrite(first, self.receipt_record(env))
            if mode == "crash":
                time.sleep(0.2)
                receipt["exit_code"] = 137
                return receipt
            end = time.monotonic() + 20
            while not ack.exists() and time.monotonic() < end and not self.controller.stopped.is_set():
                time.sleep(0.02)
            if not ack.exists():
                receipt["exit_code"] = 143
                return receipt
            if mode == "hang_after_ack":
                self.controller.stopped.wait(60)
                receipt["exit_code"] = 143
                return receipt
            if mode == "native":
                jwrite(first, self.receipt_record(env, kind="observed_native_tool"))
            status = {"timeout": "timeout", "error": "error"}.get(mode, "ok")
            rc = {"timeout": 124, "error": 1}.get(mode, 0)
            self.write_summary(env, status, rc)
            jwrite(sf, self.state_record(env, status="finished", result=status))
            return receipt
        finally:
            self.controller.deactivate(unit)


class EnvTestCase(unittest.TestCase):
    def env(self, mode="ok"):
        return FakeRootEnv(self, mode)

    def run_to_end(self, env, job_id="job-1"):
        run = env.begin(job_id)
        ev = run.wait_bootstrap(10)
        if ev is not None:
            run.acknowledge_start({"status": "started"})
        self.assertTrue(run.wait_done(20))
        return run, ev


class TestEntrypointValidation(unittest.TestCase):
    def test_rejects_relative_path(self):
        with self.assertRaises(DispatchError) as cm:
            validate_entrypoint("scripts/root/headless-root-run.sh", allowed_repo_root="/tmp")
        self.assertIn("entrypoint_not_absolute", str(cm.exception))

    def test_rejects_dotdot(self):
        with self.assertRaises(DispatchError) as cm:
            validate_entrypoint("/tmp/../tmp/x.sh", allowed_repo_root="/tmp")
        self.assertIn("dotdot", str(cm.exception))

    def test_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            real = pathlib.Path(d) / "real.sh"
            real.write_text("#!/bin/bash\n")
            real.chmod(0o755)
            link = pathlib.Path(d) / "link.sh"
            link.symlink_to(real)
            with self.assertRaises(DispatchError) as cm:
                validate_entrypoint(str(link), allowed_repo_root=d)
            self.assertIn("symlink_or_unresolved", str(cm.exception))

    def test_rejects_symlinked_ancestor_directory(self):
        with tempfile.TemporaryDirectory() as d:
            real_dir = pathlib.Path(d) / "real"
            real_dir.mkdir()
            script = real_dir / "x.sh"
            script.write_text("#!/bin/bash\n")
            script.chmod(0o755)
            (pathlib.Path(d) / "alias").symlink_to(real_dir)
            with self.assertRaises(DispatchError) as cm:
                validate_entrypoint(str(pathlib.Path(d) / "alias" / "x.sh"), allowed_repo_root=d)
            self.assertIn("symlink_or_unresolved", str(cm.exception))

    def test_rejects_outside_repo_root(self):
        with tempfile.TemporaryDirectory() as d:
            outside = pathlib.Path(d) / "elsewhere" / "x.sh"
            outside.parent.mkdir()
            outside.write_text("#!/bin/bash\n")
            outside.chmod(0o755)
            with tempfile.TemporaryDirectory() as repo:
                with self.assertRaises(DispatchError) as cm:
                    validate_entrypoint(str(outside), allowed_repo_root=repo)
                self.assertIn("outside_repo_root", str(cm.exception))

    def test_rejects_missing_non_executable_directory_and_writable_by_others(self):
        with tempfile.TemporaryDirectory() as d:
            missing = pathlib.Path(d) / "missing.sh"
            with self.assertRaises(DispatchError):
                validate_entrypoint(str(missing), allowed_repo_root=d)
            plain = pathlib.Path(d) / "plain.sh"
            plain.write_text("#!/bin/bash\n")
            with self.assertRaises(DispatchError) as cm:
                validate_entrypoint(str(plain), allowed_repo_root=d)
            self.assertIn("not_executable", str(cm.exception))
            sub = pathlib.Path(d) / "adir"
            sub.mkdir()
            sub.chmod(0o755)
            with self.assertRaises(DispatchError) as cm:
                validate_entrypoint(str(sub), allowed_repo_root=d)
            self.assertIn("not_regular_file", str(cm.exception))
            loose = pathlib.Path(d) / "loose.sh"
            loose.write_text("#!/bin/bash\n")
            loose.chmod(0o777)
            with self.assertRaises(DispatchError) as cm:
                validate_entrypoint(str(loose), allowed_repo_root=d)
            self.assertIn("writable_by_others", str(cm.exception))
            loose.chmod(0o775)  # group-writable by the owner's own group is fine (private umask 002 checkouts)
            self.assertEqual(validate_entrypoint(str(loose), allowed_repo_root=d), loose.resolve())

    @unittest.skipUnless(os.path.exists(REAL_CAG_SCRIPT), "real CAG script not installed")
    def test_accepts_real_installed_cag_script(self):
        p = validate_entrypoint(REAL_CAG_SCRIPT)
        self.assertEqual(p.name, "headless-root-run.sh")

    @unittest.skipUnless(os.path.exists(REAL_CAG_SCRIPT), "real CAG script not installed")
    def test_b4_default_home_relative_path_validates_unpatched(self):
        """B4: the configured default starts with '~'; it must validate, not die as not_absolute."""
        self.assertTrue(rd.DEFAULT_SCRIPT_PATH.startswith("~"))
        self.assertEqual(str(validate_entrypoint(rd.DEFAULT_SCRIPT_PATH)), REAL_CAG_SCRIPT)
        cfg = TrustedRootConfig.build()
        self.assertEqual(str(cfg.validated_entrypoint()), REAL_CAG_SCRIPT)
        report = LauncherQualification().inspect_interfaces()
        self.assertEqual(report["entrypoint_status"], "verified")
        self.assertEqual(report["entrypoint"], REAL_CAG_SCRIPT)

    def test_b4_tilde_default_resolves_under_temp_home_and_user_forms_are_refused(self):
        with tempfile.TemporaryDirectory() as home:
            script = pathlib.Path(home) / "git" / "cloudflare-agent-git" / "scripts" / "root" / "headless-root-run.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/bash\n")
            script.chmod(0o755)
            with patch.dict(os.environ, {"HOME": str(pathlib.Path(home).resolve())}):
                self.assertEqual(validate_entrypoint(rd.DEFAULT_SCRIPT_PATH), script.resolve())
                with self.assertRaises(DispatchError) as cm:
                    validate_entrypoint("~root/x.sh", "~")
                self.assertIn("entrypoint_not_absolute", str(cm.exception))


class TestTrustedConfig(unittest.TestCase):
    def test_host_state_dir_must_not_be_canonical_root_state(self):
        with tempfile.TemporaryDirectory() as d:
            canon = pathlib.Path(d) / "headless-root"
            for host in (canon, canon / "host", pathlib.Path(d)):
                with self.assertRaises(DispatchError) as cm:
                    TrustedRootConfig.build("/x", "/x", canon, host)
                self.assertIn("conflated", str(cm.exception))

    def test_canonical_dir_follows_hr_state(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"HR_STATE": d}):
                cfg = TrustedRootConfig.build()
            self.assertEqual(cfg.canonical_state_dir, pathlib.Path(d).resolve())
            self.assertNotEqual(cfg.host_state_dir, cfg.canonical_state_dir)


class FakeHubTransport:
    def __init__(self, test, responses):
        self.test = test
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, token, timeout):
        self.calls.append((url, token, timeout))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


def hub_ok(enabled=True, server_delta=0.0, updated_at=None):
    return {"enabled": enabled, "updated_at": updated_at, "server_time": iso_now(server_delta)}


class TestHubSchedulingReader(unittest.TestCase):
    """B6: live authenticated read per new run; a local file is never authority."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.canon = self.base / "headless-root"
        self.canon.mkdir()
        self.cache_dir = self.base / "host-cache"

    def reader(self, responses, **kw):
        transport = FakeHubTransport(self, responses)
        r = HubSchedulingReader(
            "https://hub.example/", lambda: "HOST-TOKEN", host_id="h1",
            cache=SchedulingCache(self.cache_dir), canonical_state_dir=self.canon,
            http_get=transport, **kw)
        return r, transport

    def test_enabled_fresh_read_allows_and_uses_authenticated_same_origin_url(self):
        r, t = self.reader([hub_ok(True)])
        self.assertEqual(r.check(), (True, "scheduling_enabled"))
        self.assertEqual(t.calls[0][0], "https://hub.example/api/root/scheduling")
        self.assertEqual(t.calls[0][1], "HOST-TOKEN")

    def test_job_credential_is_used_when_given(self):
        r, t = self.reader([hub_ok(True)])
        self.assertTrue(r.check("JOB-TOKEN")[0])
        self.assertEqual(t.calls[0][1], "JOB-TOKEN")

    def test_refused_job_credential_falls_back_to_host_credential_and_is_still_verified(self):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        r, t = self.reader([err, hub_ok(True)])
        self.assertEqual(r.check("JOB-TOKEN"), (True, "scheduling_enabled"))
        self.assertEqual([c[1] for c in t.calls], ["JOB-TOKEN", "HOST-TOKEN"])
        r, t = self.reader([err, hub_ok(True, server_delta=-3600)])
        self.assertFalse(r.check("JOB-TOKEN")[0])
        r, t = self.reader([err, err])
        self.assertIn("scheduling_unauthorized", r.check("JOB-TOKEN")[1])
        self.assertEqual(len(t.calls), 2)

    def test_every_decision_is_a_live_read(self):
        r, t = self.reader([hub_ok(True)])
        for _ in range(3):
            self.assertTrue(r.check()[0])
        self.assertEqual(len(t.calls), 3)

    def test_disabled_denies(self):
        r, _ = self.reader([hub_ok(False)])
        ok, reason = r.check()
        self.assertFalse(ok)
        self.assertIn("scheduling_disabled", reason)

    def test_missing_credential_denies_without_request(self):
        r, t = self.reader([hub_ok(True)])
        r.token_provider = lambda: ""
        self.assertEqual(r.check(), (False, "scheduling_credential_missing"))
        self.assertEqual(t.calls, [])

    def test_unauthorized_names_the_missing_scoped_interface(self):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        r, _ = self.reader([err])
        ok, reason = r.check()
        self.assertFalse(ok)
        self.assertIn("scheduling_unauthorized", reason)
        self.assertIn("scoped host read", reason)

    def test_endpoint_absent_and_http_errors_deny(self):
        for code, text in ((404, "scheduling_endpoint_absent"), (500, "scheduling_http_error: 500")):
            r, _ = self.reader([urllib.error.HTTPError("u", code, "x", {}, None)])
            self.assertEqual(r.check(), (False, text))

    def test_unreachable_denies(self):
        r, _ = self.reader([urllib.error.URLError("down")])
        ok, reason = r.check()
        self.assertFalse(ok)
        self.assertIn("scheduling_unreachable", reason)

    def test_stale_future_naive_and_corrupt_responses_deny(self):
        bad = [
            hub_ok(True, server_delta=-3600),                       # replayed old answer
            hub_ok(True, server_delta=3600),                        # impossible future
            {"enabled": True, "server_time": "2026-10-14T10:00:00"},  # naive timestamp
            {"enabled": "true", "server_time": iso_now()},          # non-bool
            {"enabled": 1, "server_time": iso_now()},
            {"server_time": iso_now()},
            {"enabled": True},
            {"enabled": True, "server_time": iso_now(), "updated_at": "garbage"},
            {"enabled": True, "server_time": iso_now(), "updated_at": iso_now(3600)},
            ["enabled"],
            None,
        ]
        for resp in bad:
            r, _ = self.reader([resp])
            self.assertFalse(r.check()[0], resp)

    def test_regressed_server_time_denies(self):
        r, _ = self.reader([hub_ok(True), hub_ok(True, server_delta=-60)])
        self.assertTrue(r.check()[0])
        self.assertEqual(r.check(), (False, "scheduling_response_regressed"))

    def test_verified_read_is_cached_privately_in_host_dir_not_canonical_state(self):
        r, _ = self.reader([hub_ok(True)])
        self.assertTrue(r.check("JOB-TOKEN-XYZ")[0])
        path = self.cache_dir / "scheduling.json"
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache_dir).st_mode), 0o700)
        self.assertEqual(list(self.canon.iterdir()), [])
        text = path.read_text()
        self.assertNotIn("TOKEN", text)
        self.assertEqual(json.loads(text)["origin"], "https://hub.example")

    def test_counterfeit_cache_is_never_authority(self):
        """A local file saying enabled cannot grant dispatch (missing/unreachable/stale hub)."""
        SchedulingCache(self.cache_dir).store({"enabled": True, "captured_at": iso_now(), "server_time": iso_now()})
        for resp in (urllib.error.URLError("down"), urllib.error.HTTPError("u", 404, "x", {}, None),
                     hub_ok(True, server_delta=-3600)):
            r, _ = self.reader([resp])
            self.assertFalse(r.check()[0])
        # No reader at all: nothing to ask, nothing allowed.
        r = HubSchedulingReader("https://hub.example", lambda: "t", http_get=FakeHubTransport(self, [urllib.error.URLError("x")]),
                                cache=SchedulingCache(self.cache_dir), canonical_state_dir=self.canon)
        self.assertFalse(r.check()[0])

    def test_fresh_cached_disabled_adds_denial_reason_only(self):
        SchedulingCache(self.cache_dir).store({"enabled": False, "captured_at": iso_now()})
        r, _ = self.reader([urllib.error.URLError("down")])
        ok, reason = r.check()
        self.assertFalse(ok)
        self.assertIn("scheduling_disabled", reason)

    def test_old_cached_disabled_is_ignored_and_in_process_observation_expires(self):
        SchedulingCache(self.cache_dir, ttl_sec=60).store({"enabled": False, "captured_at": iso_now(-3600)})
        self.assertIsNone(SchedulingCache(self.cache_dir, ttl_sec=60).load_denial(time.time()))
        r, _ = self.reader([hub_ok(True)], ttl_sec=0.05)
        self.assertTrue(r.check()[0])
        self.assertTrue(r.last_observation())
        time.sleep(0.1)
        self.assertIsNone(r.last_observation())

    def test_cache_dir_conflated_with_canonical_state_is_refused(self):
        with self.assertRaises(DispatchError) as cm:
            HubSchedulingReader("https://hub.example", lambda: "t", cache=SchedulingCache(self.canon),
                                canonical_state_dir=self.canon, http_get=lambda *a: {})
        self.assertIn("conflated", str(cm.exception))
        with self.assertRaises(DispatchError):
            HubSchedulingReader("https://hub.example", lambda: "t", cache=SchedulingCache(self.canon / "sub"),
                                canonical_state_dir=self.canon, http_get=lambda *a: {})

    def test_cache_refuses_symlinked_dir_and_file(self):
        real = self.base / "real"
        real.mkdir()
        link = self.base / "link"
        link.symlink_to(real)
        with self.assertRaises(DispatchError):
            SchedulingCache(link).store({"enabled": True})
        cache = SchedulingCache(self.cache_dir)
        cache.store({"enabled": False, "captured_at": iso_now()})
        os.unlink(cache.path)
        (self.cache_dir / "elsewhere.json").write_text("{}")
        cache.path.symlink_to(self.cache_dir / "elsewhere.json")
        self.assertIsNone(cache.load_denial(time.time()))


class _HubHandler(http.server.BaseHTTPRequestHandler):
    seen = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if type(self).mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()
            return
        body = json.dumps(hub_ok(True)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestHubSchedulingDefaultTransport(unittest.TestCase):
    def setUp(self):
        _HubHandler.seen = []
        _HubHandler.mode = "ok"
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _HubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_real_http_get_sends_bearer_to_exact_path(self):
        r = HubSchedulingReader(self.url, lambda: "HOST-TOKEN")
        self.assertEqual(r.check(), (True, "scheduling_enabled"))
        self.assertEqual(_HubHandler.seen, [("/api/root/scheduling", "Bearer HOST-TOKEN")])

    def test_redirect_is_refused(self):
        _HubHandler.mode = "redirect"
        r = HubSchedulingReader(self.url, lambda: "HOST-TOKEN")
        ok, reason = r.check()
        self.assertFalse(ok)
        self.assertEqual(len(_HubHandler.seen), 1)

    def test_non_https_remote_origin_denies_without_request(self):
        r = HubSchedulingReader("http://hub.example.com", lambda: "HOST-TOKEN")
        ok, reason = r.check()
        self.assertFalse(ok)
        self.assertIn("scheduling_unreachable", reason)


class TempStateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()

    def write_state(self, data):
        (self.state_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")

    def status(self, **kw):
        return canonical_incumbent_status(str(self.state_dir), **kw)[0]


class TestCanonicalIncumbent(TempStateCase):
    def setUp(self):
        super().setUp()
        # The /proc/locks reading is only authoritative from the init PID namespace (F2); these
        # tests exercise the incumbent rules themselves. TestB7InitPidNamespaceLockGuard covers the guard.
        patcher = patch.object(rd, "in_init_pid_namespace", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_none_when_no_state(self):
        status, state = canonical_incumbent_status(str(self.state_dir))
        self.assertEqual(status, "none")
        self.assertEqual(state, {})

    def test_healthy_incumbent(self):
        now = int(time.time())
        self.write_state({"status": "running", "pid": os.getpid(), "heartbeat": now,
                          "start": now, "run_id": "r1", "epoch": 3})
        status, state = canonical_incumbent_status(str(self.state_dir))
        self.assertEqual(status, "healthy")
        self.assertEqual(state.get("run_id"), "r1")

    def test_stale_heartbeat(self):
        now = int(time.time())
        self.write_state({"status": "running", "pid": os.getpid(), "heartbeat": now - 400,
                          "start": now - 400, "run_id": "r2", "epoch": 4})
        self.assertEqual(self.status(), "stale")

    def test_future_timestamp_treated_stale(self):
        now = int(time.time())
        self.write_state({"status": "running", "pid": os.getpid(), "heartbeat": now + 600,
                          "start": now, "run_id": "r3", "epoch": 5})
        self.assertEqual(self.status(), "stale")

    def test_read_canonical_state_corrupt_returns_empty(self):
        (self.state_dir / "state.json").write_text("]]]", encoding="utf-8")
        self.assertEqual(read_canonical_state(str(self.state_dir)), {})

    def test_b7_corrupt_unreadable_empty_and_unknown_status_hold(self):
        (self.state_dir / "state.json").write_text("]]]", encoding="utf-8")
        self.assertEqual(self.status(), "unknown")
        (self.state_dir / "state.json").write_text("[1]", encoding="utf-8")
        self.assertEqual(self.status(), "unknown")
        (self.state_dir / "state.json").unlink()
        (self.state_dir / "state.json").mkdir()  # exists but cannot be read as a file
        self.assertEqual(self.status(), "unknown")
        (self.state_dir / "state.json").rmdir()
        self.write_state({})
        self.assertEqual(self.status(), "unknown")
        self.write_state({"status": "levitating", "run_id": "r"})
        self.assertEqual(self.status(), "unknown")

    def test_b7_foreign_pid_namespace_holds(self):
        now = int(time.time())
        self.write_state({"status": "running", "pid": os.getpid(), "heartbeat": now, "start": now,
                          "run_id": "r", "pid_namespace": "pid:[1]"})
        self.assertEqual(self.status(), "unknown")

    def test_b7_alive_pid_with_unreadable_identity_holds_and_dead_pid_is_crashed(self):
        now = int(time.time())
        self.write_state({"status": "running", "pid": os.getpid(), "heartbeat": now, "start": now, "run_id": "r"})
        self.assertEqual(self.status(proc_probe=lambda pid: None), "unknown")
        child = subprocess.Popen(["true"])
        child.wait()
        self.write_state({"status": "running", "pid": child.pid, "heartbeat": now, "start": now, "run_id": "r"})
        self.assertEqual(self.status(), "crashed")

    def test_b7_pid_reuse_start_ticks_mismatch_is_not_healthy(self):
        now = int(time.time())
        self.write_state({"status": "running", "pid": os.getpid(), "heartbeat": now, "start": now, "run_id": "r",
                          "pid_start_ticks": 1})
        self.assertEqual(self.status(), "crashed")

    def test_b7_lock_held_while_not_running_is_starting_not_none(self):
        self.write_state({"status": "finished", "run_id": "prev"})
        fd = os.open(self.state_dir / "root.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.status(), "starting")
        finally:
            os.close(fd)
        self.assertEqual(self.status(), "none")

    def test_b7_lock_held_without_state_is_starting(self):
        fd = os.open(self.state_dir / "root.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.status(), "starting")
        finally:
            os.close(fd)

    def test_b7_unknown_lock_state_holds(self):
        self.write_state({"status": "finished", "run_id": "prev"})
        self.assertEqual(self.status(lock_probe=lambda p: None), "unknown")

    def test_b7_inspection_never_takes_or_probes_the_flock(self):
        """No flock probe: the timer's own `flock -n` must never see the host as a holder."""
        self.write_state({"status": "finished", "run_id": "prev"})
        (self.state_dir / "root.lock").write_text("")
        with patch.object(fcntl, "flock", side_effect=AssertionError("host must not flock")), \
             patch.object(fcntl, "lockf", side_effect=AssertionError("host must not lockf")):
            self.assertEqual(self.status(), "none")
        src = pathlib.Path(rd.__file__).read_text()
        self.assertNotIn("flock(", src.replace("flock -n", ""))
        self.assertNotIn("import fcntl", src)
        fd = os.open(self.state_dir / "root.lock", os.O_RDWR)
        try:
            self.status()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # still takeable right after inspection
        finally:
            os.close(fd)


class TestReceiptValidation(EnvTestCase):
    """B2: only a receipt produced by THIS run, bound to state and live facts, is evidence."""

    def start_unit(self, env):
        """Return (run, request_env) with the simulated unit hung before it writes anything."""
        env.mode = "hang_before_receipt"
        run = env.begin()
        deadline = time.time() + 5
        while not env.requests and time.time() < deadline:
            time.sleep(0.01)
        self.addCleanup(lambda: (env.controller.stopped.set(), run.wait_done(5)))
        return run, env.requests[0].job_env

    def validate(self, env, run, receipt=None, state=None, **kw):
        jenv = env.requests[0].job_env
        if state is not False:
            jwrite(env.state / "state.json", state if state is not None else env.state_record(jenv))
        path = run.expected.first_action_file
        if receipt is not None:
            jwrite(path, receipt)
        return rd.validate_run_receipt(path, run.expected, env.state, time.time(), env.proc_probe,
                                       kw.get("invocation_probe", env.adapter().invocation_probe),
                                       get_current_pid_namespace())

    def test_genuine_bootstrap_receipt_validates_and_is_labelled_bootstrap(self):
        env = self.env()
        run, jenv = self.start_unit(env)
        env.controller.activate(run.expected.unit, env.invocation)
        ev = self.validate(env, run, env.receipt_record(jenv))
        self.assertEqual(ev["kind"], "observed_root_bootstrap")
        self.assertIsNone(ev["native_session_id"])

    def test_tampered_receipts_are_refused(self):
        env = self.env()
        run, jenv = self.start_unit(env)
        env.controller.activate(run.expected.unit, env.invocation)
        good = env.receipt_record(jenv)
        cases = {
            "foreign_run": ({**good, "run_id": "OLD-TIMER-RUN"}, "mismatch: run_id"),
            "wrong_job": ({**good, "job_id": "other"}, "mismatch: job_id"),
            "wrong_host": ({**good, "host_id": "other"}, "mismatch: host_id"),
            "wrong_epoch": ({**good, "hub_lease_epoch": 99}, "mismatch: hub_lease_epoch"),
            "wrong_task": ({**good, "launcher_task_id": "x"}, "mismatch: launcher_task_id"),
            "wrong_unit": ({**good, "launcher_unit": "x.service"}, "mismatch: launcher_unit"),
            "wrong_entry_hash": ({**good, "source_entrypoint_sha256": "b" * 64}, "source_entrypoint_sha256"),
            "bad_digest": ({**good, "actual_action_digest": "xyz"}, "actual_action_digest"),
            "empty_action": ({**good, "actual_action": ""}, "actual_action"),
            "schema": ({**good, "schema_version": 2}, "schema_unsupported"),
            "bool_schema": ({**good, "schema_version": True}, "schema_unsupported"),
            "kind": ({**good, "kind": "first_action"}, "kind_unknown"),
            "stale_time": ({**good, "observed_at_utc": iso_now(-3600)}, "outside_dispatch_window"),
            "future_time": ({**good, "observed_at_utc": iso_now(3600)}, "outside_dispatch_window"),
            "naive_time": ({**good, "observed_at_utc": "2026-10-14T10:00:00"}, "observed_at_utc"),
            "bootstrap_with_session": ({**good, "native_session_id": "codex-1"}, "bootstrap_claims_native_session"),
            "native_without_session": ({**good, "kind": "observed_native_tool", "native_session_id": None},
                                       "native_session_missing"),
            "str_epoch": ({**good, "native_epoch": "7"}, "native_epoch"),
        }
        for name, (rec, needle) in cases.items():
            with self.subTest(name), self.assertRaises(DispatchError) as cm:
                self.validate(env, run, rec)
            self.assertIn(needle, str(cm.exception))

    def test_state_and_process_bindings_are_enforced(self):
        env = self.env()
        run, jenv = self.start_unit(env)
        env.controller.activate(run.expected.unit, env.invocation)
        good_state = env.state_record(jenv)
        rec = env.receipt_record(jenv)
        cases = {
            "other_run_in_state": ({**good_state, "run_id": "OLD-STALE-RUN"}, "state_not_this_run"),
            "finished_state": ({**good_state, "status": "finished"}, "state_not_this_run"),
            "state_job": ({**good_state, "hub_job_id": "x"}, "state_mismatch: hub_job_id"),
            "state_lease_epoch": ({**good_state, "hub_lease_epoch": 5}, "state_mismatch: hub_lease_epoch"),
            "state_unit": ({**good_state, "launcher_unit": "x.service"}, "state_mismatch: launcher_unit"),
            "state_invocation": ({**good_state, "launcher_invocation_id": "other"}, "state_mismatch"),
            "state_native_epoch": ({**good_state, "epoch": 8}, "state_mismatch: epoch"),
            "state_predates": ({**good_state, "start": int(time.time()) - 3600}, "predates_dispatch"),
            "pid_1": ({**good_state, "pid": 1}, "pid_invalid"),
            "host_pid": ({**good_state, "pid": os.getpid()}, "pid_invalid"),
            "fake_namespace": ({**good_state, "pid_namespace": "pid:[1]"}, "foreign_pid_namespace"),
            "no_namespace": ({k: v for k, v in good_state.items() if k != "pid_namespace"}, "foreign_pid_namespace"),
            "ticks": ({**good_state, "pid_start_ticks": 1}, "start_ticks"),
            "no_ticks": ({k: v for k, v in good_state.items() if k != "pid_start_ticks"}, "start_ticks"),
        }
        for name, (st, needle) in cases.items():
            with self.subTest(name), self.assertRaises(DispatchError) as cm:
                self.validate(env, run, rec, st)
            self.assertIn(needle, str(cm.exception))
        with self.assertRaises(DispatchError) as cm:  # unit not live / invocation differs
            self.validate(env, run, rec, invocation_probe=lambda unit: "inv-OTHER")
        self.assertIn("invocation_mismatch", str(cm.exception))
        with self.assertRaises(DispatchError) as cm:
            self.validate(env, run, rec, invocation_probe=lambda unit: None)
        self.assertIn("invocation_mismatch", str(cm.exception))
        with self.assertRaises(DispatchError) as cm:  # state.json missing entirely
            rd.validate_run_receipt(run.expected.first_action_file, run.expected, env.state / "nowhere",
                                    time.time(), env.proc_probe, None, get_current_pid_namespace())
        self.assertIn("state_unavailable", str(cm.exception))

    def test_process_outside_owned_unit_and_dead_process_are_refused(self):
        env = self.env()
        run, jenv = self.start_unit(env)
        rec = env.receipt_record(jenv)
        st = env.state_record(jenv)
        jwrite(env.state / "state.json", st)
        jwrite(run.expected.first_action_file, rec)
        foreign = lambda pid: {**(rd.read_process_identity(pid) or {}), "cgroup": "0::/user.slice/other.service"}
        with self.assertRaises(DispatchError) as cm:
            rd.validate_run_receipt(run.expected.first_action_file, run.expected, env.state, time.time(), foreign,
                                    None, get_current_pid_namespace())
        self.assertIn("not_in_owned_unit", str(cm.exception))
        with self.assertRaises(DispatchError) as cm:
            rd.validate_run_receipt(run.expected.first_action_file, run.expected, env.state, time.time(),
                                    lambda pid: None, None, get_current_pid_namespace())
        self.assertIn("not_live", str(cm.exception))
        other_ns = lambda pid: {**(rd.read_process_identity(pid) or {}), "namespace": "pid:[1]",
                                "cgroup": run.expected.unit}
        with self.assertRaises(DispatchError) as cm:
            rd.validate_run_receipt(run.expected.first_action_file, run.expected, env.state, time.time(), other_ns,
                                    None, get_current_pid_namespace())
        self.assertIn("foreign_pid_namespace", str(cm.exception))

    def test_receipt_file_hygiene(self):
        env = self.env()
        run, jenv = self.start_unit(env)
        env.controller.activate(run.expected.unit, env.invocation)
        path = run.expected.first_action_file
        jwrite(env.state / "state.json", env.state_record(jenv))
        jwrite(path, env.receipt_record(jenv), mode=0o666)
        with self.assertRaises(DispatchError) as cm:
            self.validate(env, run)
        self.assertIn("writable_by_others", str(cm.exception))
        os.unlink(path)
        target = env.base / "elsewhere.json"
        jwrite(target, env.receipt_record(jenv))
        path.symlink_to(target)
        with self.assertRaises(DispatchError) as cm:
            self.validate(env, run)
        self.assertIn("not_regular", str(cm.exception))
        os.unlink(path)
        path.write_text("x" * (rd.MAX_RECEIPT_BYTES + 1))
        os.chmod(path, 0o600)
        with self.assertRaises(DispatchError) as cm:
            self.validate(env, run)
        self.assertIn("too_large", str(cm.exception))
        path.write_text("[not an object")
        os.chmod(path, 0o600)
        with self.assertRaises(DispatchError) as cm:
            self.validate(env, run)
        self.assertIn("corrupt_json", str(cm.exception))


class TestRootRunProtocol(EnvTestCase):
    """B1/B2/B5: outcome from the run's own summary; zero effects for foreign state; exact-unit cancel."""

    def test_b1_completed_run_is_succeeded_not_fenced_failed(self):
        env = self.env("ok")
        run, ev = self.run_to_end(env)
        out = run.outcome()
        self.assertEqual(out["hub_status"], "succeeded")
        self.assertEqual(out["run_status"], "ok")
        self.assertNotIn("fenced", out["summary"])
        self.assertIn("native first tool: UNKNOWN", out["summary"])
        self.assertTrue(run.fixture_only)
        self.assertEqual(ev["kind"], "observed_root_bootstrap")
        self.assertTrue(any(r.endswith(f"{run.expected.run_id}.json") for r in out["evidence_refs"]))

    def test_b1_crashed_run_left_running_is_failed_never_succeeded(self):
        env = self.env("crash")
        run, ev = self.run_to_end(env)
        out = run.outcome()
        self.assertEqual(out["hub_status"], "failed")
        self.assertIn("crashed", out["summary"])

    def test_b1_crash_with_zero_exit_and_no_summary_is_failed(self):
        env = self.env("crash")
        env_exec = env.execute

        def zero_exit_crash(req):
            r = env_exec(req)
            r["exit_code"] = 0
            return r
        run = env.begin(launcher_executor=zero_exit_crash)
        run.wait_bootstrap(10)
        run.acknowledge_start({"status": "started"})
        run.wait_done(10)
        self.assertEqual(run.outcome()["hub_status"], "failed")

    def test_b1_error_and_timeout_summaries_fail_and_skips_are_cancelled_never_success(self):
        for mode, hub, run_status in (("error", "failed", "error"), ("timeout", "failed", "timeout"),
                                      ("skip", "cancelled", "skipped"), ("quota_held", "cancelled", "quota_held")):
            with self.subTest(mode):
                env = self.env(mode)
                run, ev = self.run_to_end(env, f"job-{mode}")
                out = run.outcome()
                self.assertEqual((out["hub_status"], out["run_status"]), (hub, run_status))
                self.assertNotEqual(out["hub_status"], "succeeded")
                if mode in ("skip", "quota_held"):
                    self.assertIsNone(ev)
                    self.assertIn("no Root cycle ran", out["summary"])

    def test_ok_summary_without_ack_or_receipt_is_not_success(self):
        env = self.env("ok")
        run = env.begin()
        run.wait_bootstrap(10)
        # the Hub start never happened, so no ack: the script must not have proceeded
        env.controller.stopped.set()
        self.assertTrue(run.wait_done(20))
        out = run.outcome()
        self.assertEqual(out["hub_status"], "failed")

    def test_b1_fenced_by_other_run_is_failed(self):
        env = self.env("ok")
        run = env.begin()
        run.wait_bootstrap(10)
        run.acknowledge_start({"status": "started"})
        self.assertTrue(run.wait_done(20))
        jwrite(env.state / "state.json", env.state_record(env.requests[0].job_env, run_id="SUPERSEDING-RUN"))
        self.assertEqual(run.outcome()["hub_status"], "failed")
        self.assertIn("fenced", run.outcome()["summary"])

    def test_native_first_tool_is_distinct_from_bootstrap(self):
        env = self.env("native")
        run, ev = self.run_to_end(env)
        out = run.outcome()
        self.assertEqual(ev["kind"], "observed_root_bootstrap")
        self.assertIn("native first tool observed (session codex-session-1", out["summary"])

    def test_b2_foreign_preexisting_state_and_noop_unit_yield_zero_effects(self):
        env = self.env("noop")
        jwrite(env.state / "state.json", env.state_record(
            {"HUB_EXPECTED_RUN_ID": "OLD-STALE-RUN", "HUB_JOB_ID": "old", "HUB_LEASE_EPOCH": "1",
             "HUB_LAUNCHER_TASK_ID": "t", "HUB_LAUNCHER_UNIT": "u.service"},
            start=int(time.time()) - 400, heartbeat=int(time.time()) - 400))
        self.assertEqual(canonical_incumbent_status(env.state)[0], "stale")
        run = env.begin()
        self.assertIsNone(run.wait_bootstrap(10))  # unit ended without a receipt of its own
        self.assertTrue(run.wait_done(5))
        self.assertFalse(run.expected.first_action_file.exists())
        out = run.outcome()
        self.assertEqual(out["hub_status"], "failed")
        self.assertNotIn("OLD-STALE-RUN", json.dumps(out))
        with self.assertRaises(DispatchError):
            run.acknowledge_start({"status": "started"})
        self.assertFalse(run.expected.start_ack_file.exists())

    def test_b2_receipt_for_someone_elses_run_is_refused_and_never_acknowledged(self):
        env = self.env("foreign_receipt")
        run = env.begin()
        with self.assertRaises(DispatchError) as cm:
            run.wait_bootstrap(10)
        self.assertIn("mismatch: run_id", str(cm.exception))
        env.controller.stopped.set()
        self.assertFalse(run.expected.start_ack_file.exists())
        run.cancel("test")

    def test_ack_requires_validated_receipt_and_hub_start_without_error(self):
        env = self.env("ok")
        run = env.begin()
        with self.assertRaises(DispatchError) as cm:
            run.acknowledge_start({"status": "started"})
        self.assertEqual(str(cm.exception), "ack_without_validated_receipt")
        run.wait_bootstrap(10)
        for bad in ({"error": "conflict"}, None, "ok"):
            with self.assertRaises(DispatchError) as cm:
                run.acknowledge_start(bad)
            self.assertEqual(str(cm.exception), "ack_without_hub_start")
        path = run.acknowledge_start({"status": "started"})
        ack = json.loads(pathlib.Path(path).read_text())
        self.assertEqual(ack["run_id"], run.expected.run_id)
        self.assertEqual(ack["job_id"], "job-1")
        self.assertEqual(ack["hub_lease_epoch"], 4)
        self.assertEqual(ack["native_epoch"], env.native_epoch)
        self.assertEqual(ack["first_action_digest"], rd.file_sha256(run.expected.first_action_file))
        self.assertNotIn("SECRET", json.dumps(ack))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        run.wait_done(20)

    def test_executor_receives_private_env_quse_provider_tmpdir_and_no_secret_in_argv(self):
        env = self.env("ok")
        run, _ = self.run_to_end(env)
        req = env.requests[0]
        for key in ("HUB_JOB_ID", "HUB_URL", "HUB_LEASE_EPOCH", "HUB_JOB_TOKEN", "HUB_HOST_ID", "HUB_LAUNCHER_TASK_ID",
                    "HUB_LAUNCHER_UNIT", "HUB_EXPECTED_RUN_ID", "HUB_FIRST_ACTION_FILE", "HUB_START_ACK_FILE",
                    "HR_STATE"):
            self.assertIn(key, req.job_env)
        self.assertEqual(req.job_env["HUB_JOB_TOKEN"], "SECRET-JOB-TOKEN")
        self.assertEqual(req.job_env["HR_STATE"], str(env.state))
        self.assertEqual(req.job_env["HUB_HOST_ID"], "host-1")
        self.assertEqual(req.job_env["HUB_EXPECTED_RUN_ID"], run.expected.run_id)
        self.assertNotIn("SECRET", " ".join(req.command_argv))
        self.assertNotIn("SECRET", repr(req))
        self.assertEqual(req.command_argv, ["bash", str(env.script)])
        self.assertEqual(req.provider, "codex")
        self.assertEqual(req.profile, "root-run")
        self.assertEqual(req.quse_json, {"fixture": "quse"})
        self.assertLessEqual(req.memory_mb, 1500)
        self.assertEqual(req.workspace, str(env.repo))
        self.assertTrue(pathlib.Path(req.tmpdir).is_relative_to(env.repo / ".local" / "tmp"))
        self.assertEqual(req.task_id, run.expected.task_id)
        self.assertEqual(run.expected.unit, f"agent-task-{req.task_id}.service")

    def test_begin_refusals_have_zero_effects(self):
        cases = {}
        env = self.env()
        cases["scheduling_denied"] = (env, dict(scheduling_check=lambda: (False, "scheduling_disabled: hub")),
                                      "scheduling_disabled")
        env2 = self.env()
        cases["scheduling_unbound"] = (env2, dict(scheduling_check=None), "scheduling_reader_unbound")
        env3 = self.env()
        env3.script.chmod(0o777)
        cases["bad_entrypoint"] = (env3, {}, "entrypoint_writable_by_others")
        env4 = self.env()
        cases["quota"] = (env4, dict(quota_check=lambda: (False, "codex window below safety threshold")),
                          "quota_gate_refused")
        env5 = self.env()
        now = int(time.time())
        jwrite(env5.state / "state.json", {"status": "running", "pid": os.getpid(), "heartbeat": now, "start": now,
                                           "run_id": "healthy"})
        cases["healthy"] = (env5, {}, "skip_healthy_incumbent")
        env6 = self.env()
        (env6.state / "state.json").write_text("]]]")
        cases["corrupt"] = (env6, {}, "root_state_unknown")
        env7 = self.env()
        run_id = "hub-job-1-e4"
        (env7.state / "runs" / f"{run_id}.json").write_text("{}")
        cases["preexisting"] = (env7, {}, "pre_existing_run_artifacts")
        env8 = self.env()
        env8.controller.activate("agent-task-hub-root-job-1-e4.service", "inv-x")
        cases["unit_in_use"] = (env8, {}, "unit_name_in_use")
        env9 = self.env()
        cases["quse"] = (env9, dict(quse_provider=lambda: None), "quota_evidence_unavailable")
        for name, (e, kw, needle) in cases.items():
            with self.subTest(name):
                with self.assertRaises(DispatchError) as cm:
                    e.begin(**kw)
                self.assertIn(needle, str(cm.exception))
                self.assertEqual(e.executions, 0)
                self.assertEqual(e.requests, [])
                self.assertFalse(any(p.name.endswith(".hub") for p in (e.state / "runs").iterdir()))

    def test_begin_validates_inputs(self):
        env = self.env()
        a = env.adapter()
        for args in (("", "t", 1, "u", "h"), ("j", "", 1, "u", "h"), ("j", "t", 0, "u", "h"), ("j", "t", True, "u", "h"),
                     ("j", "t", 1, "u", "")):
            with self.assertRaises(DispatchError):
                a.begin(*args)
        self.assertEqual(env.executions, 0)

    def test_dispatch_requires_the_hub_start_callback(self):
        env = self.env()
        with self.assertRaises(DispatchError) as cm:
            env.adapter().dispatch("job-1", "tok", 4, "https://hub", {}, None, "host-1")
        self.assertEqual(str(cm.exception), "start_callback_required")
        self.assertEqual(env.executions, 0)

    def test_dispatch_blocking_wrapper_end_to_end(self):
        env = self.env("ok")
        seen = []
        out = env.adapter().dispatch("job-w", "tok", 5, "https://hub", {}, None, "host-1",
                                     start_callback=lambda ev: seen.append(ev) or {"status": "started"})
        self.assertEqual(out["hub_status"], "succeeded")
        self.assertTrue(out["fixture_only"])
        self.assertEqual(len(seen), 1)

    def test_b5_cancel_stops_only_the_exact_owned_unit_and_waits(self):
        env = self.env("hang_after_ack")
        run = env.begin()
        run.wait_bootstrap(10)
        run.acknowledge_start({"status": "started"})
        env.controller.activate("agent-task-someone-else.service", "inv-foreign")
        info = run.cancel("hub_failure")
        self.assertTrue(info["verified"])
        self.assertTrue(info["signalled"])
        self.assertFalse(run.thread_alive)
        self.assertTrue(run.done)
        self.assertEqual([(u, i, v) for u, i, v in env.controller.signals][0][:2], (run.expected.unit, env.invocation))
        self.assertTrue(all(u == run.expected.unit for u, _, _ in env.controller.signals))
        self.assertEqual(env.controller.units["agent-task-someone-else.service"]["inv"], "inv-foreign")
        self.assertEqual(run.cancel("again"), info)  # idempotent

    def test_b5_cancel_before_receipt_captures_live_invocation_and_stops(self):
        env = self.env("hang_before_receipt")
        run = env.begin()
        with self.assertRaises(DispatchError) as cm:
            run.wait_bootstrap(0.3)
        self.assertIn("no_first_action_receipt", str(cm.exception))
        info = run.cancel("receipt_timeout")
        self.assertTrue(info["verified"])
        self.assertFalse(run.thread_alive)

    def test_b5_cancel_refuses_foreign_invocation_and_reports_unverified(self):
        env = self.env("hang_after_ack")
        run = env.begin()
        run.wait_bootstrap(10)
        run.acknowledge_start({"status": "started"})
        env.controller.units[run.expected.unit]["inv"] = "inv-REPLACED"  # unit was replaced under the same name
        info = run.cancel("hub_failure", wait_sec=1.0)
        self.assertFalse(info["verified"])
        self.assertEqual(env.controller.signals, [])
        self.assertTrue(env.controller.refused)
        # An unverified cancel is not final: once the unit is genuinely the owned one again, a retry works.
        env.controller.units[run.expected.unit]["inv"] = env.invocation
        again = run.cancel("retry", wait_sec=5.0)
        self.assertTrue(again["verified"])
        self.assertEqual([(u, i) for u, i, _ in env.controller.signals][0], (run.expected.unit, env.invocation))

    def test_summary_for_the_wrong_native_epoch_is_not_success(self):
        env = self.env("ok")
        run = env.begin()
        run.wait_bootstrap(10)
        run.acknowledge_start({"status": "started"})
        run.wait_done(20)
        jwrite(env.state / "runs" / f"{run.expected.run_id}.json",
               {"run_id": run.expected.run_id, "epoch": 999, "status": "ok", "exit_code": 0})
        self.assertEqual(run.outcome()["hub_status"], "failed")

    def test_b5_cancel_with_unit_that_never_appears_signals_nothing(self):
        env = self.env("ok")
        started = threading.Event()
        release = threading.Event()

        def slow_start(req):
            started.set()
            release.wait(10)
            return {"unit_name": req.job_env["HUB_LAUNCHER_UNIT"], "exit_code": 0, "invocation_id": "i",
                    "cleanup_verified": True}
        run = env.begin(launcher_executor=slow_start)
        started.wait(5)
        info = run.cancel("test", wait_sec=0.5)
        self.assertFalse(info["verified"])
        self.assertFalse(info["signalled"])
        self.assertEqual(env.controller.signals, [])
        release.set()
        run.wait_done(5)

    def test_executor_failure_is_reported_with_the_token_scrubbed(self):
        env = self.env("ok")

        def boom(req):
            raise RuntimeError(f"launcher blew up with {req.job_env['HUB_JOB_TOKEN']}")
        run = env.begin(launcher_executor=boom)
        with self.assertRaises(DispatchError) as cm:
            run.wait_bootstrap(5)
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertIn("[redacted]", str(cm.exception))
        self.assertNotIn("SECRET", run.outcome()["summary"])
        self.assertEqual(run.outcome()["hub_status"], "failed")

    def test_receipt_missing_actual_fields_or_wrong_unit_fails_the_outcome(self):
        for rec in ({"ok": True}, {"unit_name": "x.service", "exit_code": 0},
                    {"MainPID": 4242, "exit_code": 0}, {"unit_name": "U", "exit_code": "0"}):
            env = self.env("noop")

            def ex(req, rec=rec):
                time.sleep(0.05)
                return {**rec, "unit_name": req.job_env["HUB_LAUNCHER_UNIT"]} if rec.get("unit_name") == "U" else dict(rec)
            run = env.begin(launcher_executor=ex)
            run.wait_done(5)
            out = run.outcome()
            self.assertEqual(out["hub_status"], "failed", rec)
            self.assertIn("launcher_receipt_missing_actual_fields", out["summary"])

    def test_real_style_receipt_requires_cleanup_and_invocation(self):
        env = self.env("noop")

        def ex(req):
            return {"unit_name": req.job_env["HUB_LAUNCHER_UNIT"], "exit_code": 0, "cleanup_verified": False}
        run = env.begin(launcher_executor=ex)
        run.fixture_only = False
        run.wait_done(5)
        self.assertIn("unverified", run.outcome()["summary"])


class StubLauncher:
    """Install a fake ``launcher.task_units`` in sys.modules (fixture-only)."""

    def __init__(self, fn):
        self.fn = fn

    def __enter__(self):
        pkg = types.ModuleType("launcher")
        tu = types.ModuleType("launcher.task_units")
        tu.__file__ = os.path.join(os.path.expanduser("~/git/agent-quota-launcher"), "launcher", "task_units.py")
        tu.execute_transient_task_unit = self.fn
        pkg.task_units = tu
        self.saved = {k: sys.modules.get(k) for k in ("launcher", "launcher.task_units")}
        sys.modules["launcher"] = pkg
        sys.modules["launcher.task_units"] = tu
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def make_request(tmpdir, **kw):
    args = dict(task_id="hub-root-j-e1", command_argv=["bash", "/x/headless-root-run.sh"], memory_mb=1500,
                workspace=str(tmpdir), tmpdir=str(pathlib.Path(tmpdir) / ".local" / "tmp" / "hub-root-j-e1"),
                timeout_sec=1500.0, quse_json={"codex": {"status": "ok"}}, provider="codex",
                job_env={"HUB_JOB_ID": "j", "HUB_URL": "u", "HUB_LEASE_EPOCH": "1", "HUB_JOB_TOKEN": "SECRET"})
    args.update(kw)
    return rd.LaunchRequest(**args)


class TestLauncherExecutor(unittest.TestCase):
    """B3: the default executor passes everything to the accepted launcher API or refuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_job_env_quse_provider_tmpdir_profile_and_capacity_are_passed(self):
        seen = {}

        def execute_transient_task_unit(task_id, command_argv, memory_mb, workspace, tmpdir=None, timeout_sec=1200.0,
                                        quse_json=None, provider=None, check_capacity=False, profile=None,
                                        job_env=None):
            seen.update(locals())
            return {"unit_name": "x.service", "exit_code": 0}

        with StubLauncher(execute_transient_task_unit):
            r = rd.LauncherExecutor()(make_request(self.dir))
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(seen["job_env"]["HUB_JOB_TOKEN"], "SECRET")
        self.assertEqual(seen["quse_json"], {"codex": {"status": "ok"}})
        self.assertEqual(seen["provider"], "codex")
        self.assertEqual(seen["profile"], "root-run")
        self.assertIs(seen["check_capacity"], True)
        self.assertEqual(seen["memory_mb"], 1500)
        self.assertEqual(seen["timeout_sec"], 1500.0)
        self.assertTrue(pathlib.Path(seen["tmpdir"]).is_relative_to(self.dir / ".local" / "tmp"))
        self.assertNotIn("SECRET", " ".join(seen["command_argv"]))

    def test_job_env_file_variant_writes_a_private_file_and_removes_it(self):
        seen = {}

        def execute_transient_task_unit(task_id, command_argv, memory_mb, workspace, tmpdir=None, timeout_sec=1.0,
                                        quse_json=None, provider=None, check_capacity=False, profile=None,
                                        job_env_file=None):
            p = pathlib.Path(job_env_file)
            seen.update(mode=stat.S_IMODE(os.stat(p).st_mode), text=p.read_text(), path=p,
                        dir_mode=stat.S_IMODE(os.stat(p.parent).st_mode), argv=command_argv)
            return {"unit_name": "x.service", "exit_code": 0}

        with StubLauncher(execute_transient_task_unit):
            rd.LauncherExecutor()(make_request(self.dir))
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["dir_mode"], 0o700)
        self.assertIn("HUB_JOB_TOKEN=SECRET", seen["text"])
        self.assertFalse(seen["path"].exists())
        self.assertNotIn("SECRET", " ".join(seen["argv"]))

    def test_missing_job_env_or_profile_support_refuses_and_never_calls_the_launcher(self):
        calls = []

        def old_api(task_id, command_argv, memory_mb, workspace, tmpdir=None, timeout_sec=1200.0, quse_json=None,
                    provider=None, check_capacity=False):
            calls.append(1)
            return {"unit_name": "x.service", "exit_code": 0}

        with StubLauncher(old_api):
            with self.assertRaises(DispatchError) as cm:
                rd.LauncherExecutor()(make_request(self.dir))
        self.assertIn("launcher_root_boundary_unsupported", str(cm.exception))
        self.assertIn("profile", str(cm.exception))
        self.assertIn("job_env|job_env_file", str(cm.exception))
        self.assertEqual(calls, [])

    def test_catch_all_kwargs_does_not_count_as_support(self):
        calls = []

        def catch_all(*args, **kwargs):
            calls.append(kwargs)
            return {"unit_name": "x.service", "exit_code": 0}

        with StubLauncher(catch_all):
            with self.assertRaises(DispatchError) as cm:
                rd.LauncherExecutor()(make_request(self.dir))
        self.assertIn("launcher_root_boundary_unsupported", str(cm.exception))
        self.assertEqual(calls, [])

    def test_launcher_without_the_api_or_receipt_not_dict(self):
        with StubLauncher(None):
            sys.modules["launcher.task_units"].execute_transient_task_unit = None
            delattr(sys.modules["launcher.task_units"], "execute_transient_task_unit")
            with self.assertRaises(DispatchError) as cm:
                rd.LauncherExecutor()(make_request(self.dir))
            self.assertIn("launcher_runtime_incomplete", str(cm.exception))

        def execute_transient_task_unit(task_id, command_argv, memory_mb, workspace, tmpdir, timeout_sec, quse_json,
                                        provider, check_capacity, profile, job_env):
            return "not a dict"

        with StubLauncher(execute_transient_task_unit):
            with self.assertRaises(DispatchError) as cm:
                rd.LauncherExecutor()(make_request(self.dir))
            self.assertIn("launcher_receipt_not_dict", str(cm.exception))

    @unittest.skipUnless(os.path.isdir(os.path.expanduser("~/git/agent-quota-launcher/launcher")),
                         "installed launcher absent")
    def test_installed_launcher_is_genuinely_unsupported_and_is_not_called(self):
        """The installed launcher has no Root boundary: UNSUPPORTED, no unit, no fabricated success."""
        with patch("subprocess.Popen", side_effect=AssertionError("must not start a unit")):
            with self.assertRaises(DispatchError) as cm:
                rd.LauncherExecutor()(make_request(self.dir))
        self.assertIn("launcher_root_boundary_unsupported", str(cm.exception))

    def test_static_boundary_inspection_ignores_docstrings_and_catch_alls(self):
        d = self.dir / "launcher"
        (d / "launcher").mkdir(parents=True)
        (d / "launcher" / "task_units.py").write_text(
            '"""job_env profile quse_json provider"""\n'
            "def execute_transient_task_unit(task_id, **kwargs):\n    '''job_env'''\n    pass\n")
        b = rd.inspect_launcher_exec_boundary(d)
        self.assertTrue(b["present"])
        self.assertIn("job_env|job_env_file", b["missing"])
        self.assertIsNone(b["env_param"])
        self.assertFalse(rd.inspect_launcher_exec_boundary(self.dir / "nowhere")["present"])


class TestUnitController(unittest.TestCase):
    def test_signal_refuses_empty_or_mismatched_invocation_without_touching_the_unit(self):
        ctl = rd.SystemdUserUnitController()
        runs = []

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="InvocationID=live-1\nActiveState=active\n", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            self.assertFalse(ctl.signal("u.service", "", "stop"))
            self.assertFalse(ctl.signal("u.service", "other", "stop"))
            self.assertTrue(ctl.signal("u.service", "live-1", "stop"))
            self.assertTrue(ctl.signal("u.service", "live-1", "kill"))
            with self.assertRaises(ValueError):
                ctl.signal("u.service", "live-1", "reload")
        issued = [c for c in runs if c[:3] != ["systemctl", "--user", "show"]]
        self.assertEqual(issued[0], ["systemctl", "--user", "stop", "u.service"])
        self.assertEqual(issued[1], ["systemctl", "--user", "kill", "--signal=SIGKILL", "u.service"])
        self.assertEqual(len(issued), 2)

    def test_show_failure_is_unknown_not_inactive(self):
        with patch("subprocess.run", return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="")):
            with self.assertRaises(DispatchError) as cm:
                rd.SystemdUserUnitController().show("u.service")
        self.assertIn("unit_state_unknown", str(cm.exception))


# ---------------------------------------------------------------------------
# R2 corrections: F1 (B5 executor failure), F2 (non-init PID namespace lock), F3, F4
# ---------------------------------------------------------------------------

class StoppingUnitController(FakeUnitController):
    """Like systemd: an accepted stop/kill of the exact live invocation ends the unit."""

    def signal(self, unit, expected_invocation, verb):
        ok = super().signal(unit, expected_invocation, verb)
        if ok:
            self.deactivate(unit)
        return ok


class InvalidCleanupReceiptEnv(FakeRootEnv):
    """A fixture launcher exits with a bad receipt while its exact owned unit stays active."""

    def __init__(self, test, changes, stops=True):
        super().__init__(test, "ok")
        self.invocation = "a" * 32
        self.changes = changes
        self.controller = StoppingUnitController() if stops else FakeUnitController()

    def execute(self, req):
        receipt = super().execute(req)
        # Simulate a launcher returning without actually dissolving the owned unit.
        self.controller.activate(req.job_env["HUB_LAUNCHER_UNIT"], self.invocation)
        receipt.update(self.changes)
        return receipt


class RaiseAfterAckEnv(FakeRootEnv):
    """The launcher raises (installed task_units TaskUnitTimeoutError / TaskUnitCleanupError / generic
    execution error paths) after Hub start while the owned unit is STILL ACTIVE."""

    def __init__(self, test, mode="ok", controller=None):
        super().__init__(test, mode)
        if controller is not None:
            self.controller = controller

    def execute(self, req):
        self.requests.append(req)
        e = req.job_env
        unit = e["HUB_LAUNCHER_UNIT"]
        self.controller.activate(unit, self.invocation)
        jwrite(self.state / "state.json", self.state_record(e))
        jwrite(pathlib.Path(e["HUB_FIRST_ACTION_FILE"]), self.receipt_record(e))
        ack = pathlib.Path(e["HUB_START_ACK_FILE"])
        end = time.monotonic() + 10
        while not ack.exists() and time.monotonic() < end:
            time.sleep(0.02)
        time.sleep(0.2)
        # the unit is deliberately left active
        raise RuntimeError(f"task unit {unit} did not cleanly dissolve lingering PIDs or cgroup")


class TestB5ExecutorFailureCleanup(unittest.TestCase):
    """F1: an executor exception after Hub start must stop the exact owned unit; "verified" comes from
    a live unit query (not-found / inactive / changed InvocationID), never from thread exit alone."""

    def started_run(self, env, job_id="job-f1"):
        run = env.begin(job_id)
        self.assertIsNotNone(run.wait_bootstrap(10))
        run.acknowledge_start({"status": "started"})
        self.assertTrue(run.wait_done(10))
        self.assertFalse(run.thread_alive)
        return run

    def test_cancel_after_executor_exception_stops_the_live_owned_invocation_and_verifies_live(self):
        env = RaiseAfterAckEnv(self, controller=StoppingUnitController())
        run = self.started_run(env)
        self.assertEqual(env.controller.show(run.expected.unit)["ActiveState"], "active")
        info = run.cancel("host_failure", wait_sec=5)
        self.assertEqual([(u, i, v) for u, i, v in env.controller.signals],
                         [(run.expected.unit, env.invocation, "stop")])
        self.assertTrue(info["signalled"])
        self.assertTrue(info["verified"])
        self.assertEqual(info["unit_state"], "not-found")
        self.assertEqual(env.controller.show(run.expected.unit)["ActiveState"], "inactive")

    def test_cancel_never_verifies_from_thread_exit_alone(self):
        env = RaiseAfterAckEnv(self)  # signals are accepted but the unit stays active
        run = self.started_run(env)
        info = run.cancel("host_failure", wait_sec=0.6, attempts=1)
        self.assertFalse(run.thread_alive)
        self.assertFalse(info["thread_alive"])
        self.assertFalse(info["verified"])
        self.assertEqual(info["unit_state"], "live")
        self.assertEqual([v for _, _, v in env.controller.signals][:1], ["stop"])
        self.assertIn("kill", [v for _, _, v in env.controller.signals])
        self.assertTrue(all(u == run.expected.unit and i == env.invocation for u, i, _ in env.controller.signals))
        self.assertEqual(env.controller.show(run.expected.unit)["ActiveState"], "active")
        self.assertIsNone(run._cancelled, "an unverified cancel is not final")

    def test_cancel_after_executor_exception_leaves_a_changed_invocation_alone(self):
        env = RaiseAfterAckEnv(self, controller=StoppingUnitController())
        run = self.started_run(env)
        env.controller.units[run.expected.unit]["inv"] = "inv-FOREIGN"  # replaced under the same name
        env.controller.activate("agent-task-someone-else.service", "inv-other")
        info = run.cancel("host_failure", wait_sec=1)
        self.assertTrue(info["verified"], "the owned invocation is gone (InvocationID changed)")
        self.assertEqual(info["unit_state"], "invocation_changed")
        self.assertEqual(env.controller.signals, [])
        self.assertEqual(env.controller.show(run.expected.unit)["ActiveState"], "active")
        self.assertEqual(env.controller.units["agent-task-someone-else.service"]["inv"], "inv-other")

    def test_cancel_after_executor_exception_with_unit_already_gone_signals_nothing(self):
        env = RaiseAfterAckEnv(self)
        run = self.started_run(env)
        env.controller.deactivate(run.expected.unit)
        info = run.cancel("host_failure", wait_sec=1)
        self.assertTrue(info["verified"])
        self.assertFalse(info["signalled"])
        self.assertEqual(env.controller.signals, [])

    def test_unreadable_unit_state_is_unverified(self):
        env = RaiseAfterAckEnv(self)
        run = self.started_run(env)

        def broken(unit):
            raise DispatchError("unit_state_unknown: systemctl show rc=1")
        env.controller.show = broken
        info = run.cancel("host_failure", wait_sec=0.5, attempts=1)
        self.assertFalse(info["verified"])
        self.assertIn("unit_state_unknown", info["error"])
        self.assertEqual(env.controller.signals, [])

    def test_cleanup_after_exit_covers_executor_exception_and_unverified_receipts_only(self):
        env = RaiseAfterAckEnv(self, controller=StoppingUnitController())
        run = self.started_run(env)
        self.assertTrue(run.cleanup_unverified)
        info = run.cleanup_after_exit()
        self.assertTrue(info["verified"])
        self.assertEqual(env.controller.signals[0][2], "stop")

        clean = FakeRootEnv(self, "ok")
        run = clean.begin("job-clean")
        run.wait_bootstrap(10)
        run.acknowledge_start({"status": "started"})
        self.assertTrue(run.wait_done(20))
        self.assertFalse(run.cleanup_unverified)
        self.assertIsNone(run.cleanup_after_exit())

        for rec in ({"unit_name": None, "exit_code": 0, "invocation_id": "inv-0001", "cleanup_verified": False},
                    {"unit_name": None, "exit_code": 0, "cleanup_verified": True},
                    None):
            env2 = FakeRootEnv(self, "noop")
            env2.controller = StoppingUnitController()

            def ex(req, rec=rec, env2=env2):
                env2.controller.activate(req.job_env["HUB_LAUNCHER_UNIT"], "inv-0001")
                if rec is None:
                    return "not a receipt"
                return {**rec, "unit_name": req.job_env["HUB_LAUNCHER_UNIT"]}
            run = env2.begin("job-r", launcher_executor=ex)
            run.fixture_only = False
            self.assertTrue(run.wait_done(5))
            self.assertTrue(run.cleanup_unverified, rec)
            info = run.cleanup_after_exit(wait_sec=2)
            self.assertTrue(info["verified"], rec)
            self.assertEqual(env2.controller.signals[0][2], "stop", rec)


class TestB5InvalidCleanupReceipts(unittest.TestCase):
    def started_run(self, env):
        run = env.begin("job-receipt")
        run.fixture_only = False  # exercise production receipt checks with fixture bindings only
        self.assertIsNotNone(run.wait_bootstrap(5))
        run.acknowledge_start({"status": "started"})
        self.assertTrue(run.wait_done(5))
        self.assertEqual(run._owned_invocation, env.invocation)
        return run

    def test_invalid_receipts_cancel_exact_owned_invocation_and_report_live_verification(self):
        cases = (
            {"unit_name": None}, {"unit_name": "agent-task-other.service"},
            {"invocation_id": "b" * 32}, {"invocation_id": None},
            {"invocation_id": ""}, {"invocation_id": "g" * 32},
            {"invocation_id": "a" * 31}, {"invocation_id": 123},
            {"exit_code": None}, {"exit_code": True}, {"exit_code": "0"},
            {"cleanup_verified": False}, {"cleanup_verified": 1},
        )
        for changes in cases:
            for stops in (True, False):
                with self.subTest(changes=changes, stops=stops):
                    env = InvalidCleanupReceiptEnv(self, changes, stops=stops)
                    env.controller.activate("agent-task-unrelated.service", "c" * 32)
                    run = self.started_run(env)
                    self.assertTrue(run.cleanup_unverified)
                    info = run.cleanup_after_exit(wait_sec=0.2, attempts=1)
                    self.assertEqual(info["verified"], stops)
                    self.assertTrue(env.controller.signals)
                    self.assertTrue(all(u == run.expected.unit and i == env.invocation
                                        for u, i, _ in env.controller.signals))
                    self.assertEqual(env.controller.show("agent-task-unrelated.service")["ActiveState"], "active")
                    self.assertEqual(run.outcome()["hub_status"], "failed")
                    if not stops:
                        self.assertEqual(info["unit_state"], "live")
                        self.assertIsNone(run._cancelled)

    def test_valid_owned_cleanup_receipt_skips_cancellation_and_preserves_outcome(self):
        env = InvalidCleanupReceiptEnv(self, {})
        # A genuine clean exit deactivates the fixture unit before returning its valid receipt.
        execute = env.execute
        def clean(req):
            receipt = execute(req)
            env.controller.deactivate(req.job_env["HUB_LAUNCHER_UNIT"])
            return receipt
        env.execute = clean
        run = self.started_run(env)
        self.assertFalse(run.cleanup_unverified)
        self.assertIsNone(run.cleanup_after_exit(wait_sec=0.2, attempts=1))
        self.assertEqual(env.controller.signals, [])
        self.assertEqual(run.outcome()["hub_status"], "succeeded")

    def test_production_invocation_format_is_required_even_before_owned_id_is_known(self):
        for invocation in (None, "", "inv-0001", "z" * 32, "a" * 31, "a" * 33, 123):
            with self.subTest(invocation=invocation):
                env = InvalidCleanupReceiptEnv(self, {})
                run = self.started_run(env)
                run._owned_invocation = None
                run._receipt["invocation_id"] = invocation
                self.assertTrue(run.cleanup_unverified)
                self.assertEqual(run.outcome()["hub_status"], "failed")
                info = run.cleanup_after_exit(wait_sec=0.2, attempts=1)
                self.assertTrue(info["verified"])
                self.assertEqual(env.controller.signals[0][:2], (run.expected.unit, env.invocation))


class TestB7InitPidNamespaceLockGuard(TempStateCase):
    """F2: /proc/locks omits flock(1) locks whose owner pid cannot be translated into a non-init
    PID namespace. The lock then reads "free" although the kernel holds it. Only an init-namespace
    reader may answer; everywhere else the answer is UNKNOWN. Private temp file only: no canonical
    runtime, no flock probe of the real lock."""

    def hold_like_the_real_script(self, path):
        import shutil
        if shutil.which("flock") is None or shutil.which("bash") is None:
            self.skipTest("flock(1) absent")
        p = subprocess.Popen(["bash", "-c", 'exec 9>>"$1"; flock -n 9 || exit 3; echo held; exec sleep 60',
                              "bash", str(path)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), "held")

    def kernel_says_held(self, path):
        fd = os.open(path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def test_flock1_lock_with_dead_owner_pid_is_never_reported_free(self):
        lock = self.state_dir / "root.lock"
        self.hold_like_the_real_script(lock)
        self.assertTrue(self.kernel_says_held(lock))
        held = rd.root_lock_held(lock)
        self.assertIsNot(held, False, "a lock the kernel holds must never read as free")
        if rd.in_init_pid_namespace() is not True:
            self.assertIsNone(held)
        self.write_state({"status": "finished", "run_id": "prev"})
        self.assertIn(self.status(), ("starting", "unknown"))
        self.assertNotEqual(self.status(), "none")

    def test_answer_requires_an_actual_init_pid_namespace(self):
        lock = self.state_dir / "root.lock"
        lock.write_text("")
        with patch.object(rd, "in_init_pid_namespace", return_value=False):
            self.assertIsNone(rd.root_lock_held(lock))
        with patch.object(rd, "in_init_pid_namespace", return_value=None):
            self.assertIsNone(rd.root_lock_held(lock))
        with patch.object(rd, "in_init_pid_namespace", return_value=True):
            self.assertFalse(rd.root_lock_held(lock))
        with patch.object(rd, "in_init_pid_namespace", return_value=False):
            self.assertFalse(rd.root_lock_held(self.state_dir / "absent.lock"), "no lock file: nothing can hold it")

    def test_non_init_namespace_with_unreadable_lock_holds_instead_of_none(self):
        self.write_state({"status": "finished", "run_id": "prev"})
        (self.state_dir / "root.lock").write_text("")
        with patch.object(rd, "in_init_pid_namespace", return_value=False):
            self.assertEqual(self.status(), "unknown")

    def test_init_namespace_needs_the_initial_pid_namespace_id_and_a_single_nspid_entry(self):
        f, ns = self.base / "status", self.base / "ns-pid"
        os.symlink(rd.INIT_PID_NAMESPACE, ns)
        for text, want in (("Name:\tx\nNSpid:\t1234\n", True), ("NSpid:\t1234\t7\n", False),
                           ("NSpid:\t1234\t7\t1\n", False), ("Name:\tx\n", None), ("NSpid:\tabc\n", None),
                           ("NSpid:\n", None)):
            f.write_text(text)
            self.assertIs(rd.in_init_pid_namespace(str(f), str(ns)), want, text)
        f.write_text("NSpid:\t1234\n")
        private = self.base / "private-ns"
        os.symlink("pid:[4026536195]", private)  # a private namespace whose own procfs shows one NSpid entry
        self.assertIs(rd.in_init_pid_namespace(str(f), str(private)), False)
        self.assertIsNone(rd.in_init_pid_namespace(str(self.base / "missing"), str(ns)))
        self.assertIsNone(rd.in_init_pid_namespace(str(f), str(self.base / "missing-ns")))


class TestF3InjectedSeamsAreFixtures(unittest.TestCase):
    SEAMS = ("proc_probe", "invocation_probe", "host_namespace", "unit_controller", "quse_provider")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        env = FakeRootEnv(self, "ok")
        self.config = env.config

    def test_every_injected_probe_or_controller_marks_the_adapter_fixture_only(self):
        seam_values = {"proc_probe": lambda pid: None, "invocation_probe": lambda unit: None,
                       "host_namespace": "pid:[1]", "unit_controller": FakeUnitController(),
                       "quse_provider": lambda: {}}
        self.assertFalse(RootDispatchAdapter(config=self.config).is_fixture_executor)
        for name in self.SEAMS:
            adapter = RootDispatchAdapter(config=self.config, **{name: seam_values[name]})
            self.assertTrue(adapter.is_fixture_executor, name)


    def test_injected_clock_is_fixture_only_even_with_production_controller_defaults(self):
        class FrozenClock:
            def time(self):
                return 1_000_000_000.0
        for clock in (FrozenClock(), time):
            with self.subTest(clock=clock):
                adapter = RootDispatchAdapter(config=self.config, clock=clock)
                self.assertTrue(adapter.is_fixture_executor)
                self.assertIn("clock", adapter._fixture_seams)
                self.assertIs(adapter.clock, clock)
        self.assertFalse(RootDispatchAdapter(config=self.config).is_fixture_executor)


class TestF4LauncherModuleIsolation(unittest.TestCase):
    def test_cached_module_of_another_checkout_cannot_answer_for_the_executor_or_quse(self):
        foreign = types.ModuleType("launcher")
        tu = types.ModuleType("launcher.task_units")
        tu.__file__ = "/elsewhere/other-checkout/launcher/task_units.py"
        tu.execute_transient_task_unit = lambda **kw: {"unit_name": "FOREIGN"}
        adm = types.ModuleType("launcher.admission")
        adm.__file__ = "/elsewhere/other-checkout/launcher/admission.py"
        adm.fetch_quse = lambda: {"foreign": True}
        foreign.task_units, foreign.admission = tu, adm
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            repo = pathlib.Path(d) / "launcher-repo"
            (repo / "launcher").mkdir(parents=True)  # nothing importable: the cached foreign module is all there is
            with patch.dict(sys.modules, {"launcher": foreign, "launcher.task_units": tu,
                                          "launcher.admission": adm}):
                with self.assertRaises(DispatchError) as cm:
                    rd.LauncherExecutor(str(repo))(make_request(d))
                self.assertIn("launcher_module_foreign_checkout", str(cm.exception))
                with self.assertRaises(DispatchError) as cm:
                    rd.fetch_quse_default(str(repo))
                self.assertIn("launcher_module_foreign_checkout", str(cm.exception))

    def test_launcher_imports_share_one_lock_with_the_qualification_inspection(self):
        from agent_hub_host import runtime
        self.assertTrue(hasattr(rd, "LAUNCHER_IMPORT_LOCK"))
        seen = []

        def inspect():
            with runtime._inspected_launcher(pathlib.Path("/nonexistent")):
                seen.append("entered")
        t = threading.Thread(target=inspect, daemon=True)
        with rd.LAUNCHER_IMPORT_LOCK:
            t.start()
            t.join(0.3)
            self.assertEqual(seen, [], "inspection must wait for the shared lock")
        t.join(5)
        self.assertEqual(seen, ["entered"])


if __name__ == "__main__":
    unittest.main()
