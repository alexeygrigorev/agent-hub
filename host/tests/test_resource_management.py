"""Host resource management: sampler + safe watchdog (intake 4fcd3139)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_hub_host import resource_management as rm


def _native(**kw):
    base = dict(session_id="11111111-1111-4111-8111-111111111111", tag="hub-abc",
                workspace="/repo", engine="codex", profile=None, phase="running",
                last_activity_ms=1_000_000, created_at_ms=900_000, cwd=None,
                containment_empty=False, workload_cgroup=None, worker_pid=None,
                workload_pid=None, processes=None, agent_state_at_ms=None,
                headless_exec=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _entry(**kw):
    base = dict(session_id="sess-1", fleet_launch_id="launch-1", intake_id="intake-1",
                native_id="11111111-1111-4111-8111-111111111111", tag="hub-abc",
                workspace="/repo", native_created_at_ms=900_000, engine="codex",
                model="m", project="p", tier="quick", kind="goal", state="running",
                keep=False, managed=True,
                last_activity_at="1970-01-01T00:16:40Z", started_at="1970-01-01T00:15:00Z")
    base.update(kw)
    return base


class SamplerTests(unittest.TestCase):
    def test_cgroup_sampler_prefers_anon_and_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg = Path(tmp) / "scope"
            cg.mkdir()
            (cg / "memory.stat").write_text("anon 209715200\nfile 0\n")
            (cg / "memory.current").write_text("209715200\n")
            (cg / "memory.peak").write_text("314572800\n")
            (cg / "cpu.stat").write_text("usage_usec 120000000\n")
            (cg / "cgroup.procs").write_text("")
            row = {"workload_cgroup": "/sys/fs/cgroup/x", "worker_pid": None,
                   "workload_pid": None, "created_at_ms": 0}
            with patch.object(rm, "_cgroup_path", return_value=str(cg)):
                out = rm.sample_session_tree(row, 180_000_000, proc_root=tmp)
            self.assertIsNotNone(out)
            self.assertAlmostEqual(out["rss_mb"], 200.0, delta=1.0)
            self.assertAlmostEqual(out["cpu_seconds"], 120.0, delta=0.01)

    def test_proc_fallback_sums_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid, ppid in (("100", "1"), ("101", "100")):
                d = proc / pid
                d.mkdir()
                # stat: comm + state + ppid + ... + utime/stime/start
                d.joinpath("stat").write_text(
                    "%s (cmd) R %s 1 1 0 -1 0 0 0 0 0 50 50 0 0 20 0 1 0 100 0 0 0" % (pid, ppid))
                d.joinpath("smaps_rollup").write_text(
                    "Rss: 10240 kB\nPss: 5120 kB\nAnonymous: 8192 kB\n")
                d.joinpath("cgroup").write_text("0::/x\n")
            row = {"workload_cgroup": None, "worker_pid": 100,
                   "workload_pid": None, "created_at_ms": 0}
            out = rm.sample_session_tree(row, 1_000_000, proc_root=str(proc))
            self.assertIsNotNone(out)
            # 2 pids x 8192 kB anon = 16 MiB.
            self.assertAlmostEqual(out["rss_mb"], 16.0, delta=0.5)

    def test_sampler_unknown_returns_none(self):
        self.assertIsNone(rm.sample_session_tree(
            {"workload_cgroup": None, "worker_pid": None, "workload_pid": None,
             "created_at_ms": None}, 1_000_000, proc_root="/nonexistent-proc-root-xyz"))

    def test_collect_tree_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid, ppid in (("10", "1"), ("11", "10"), ("12", "11")):
                d = proc / pid
                d.mkdir()
                d.joinpath("stat").write_text(
                    "%s (c) R %s 1 1 0 -1 0 0 0 0 0 10 10 0 0 20 0 1 0 50 0 0 0" % (pid, ppid))
            self.assertEqual(rm.collect_tree_pids([10], str(proc)), [10, 11, 12])


class GateTests(unittest.TestCase):
    def test_reapable_filters(self):
        good = _entry()
        self.assertTrue(rm.is_reapable_session(good))
        self.assertFalse(rm.is_reapable_session(_entry(managed=False)))
        self.assertFalse(rm.is_reapable_session(_entry(keep=True)))
        self.assertFalse(rm.is_reapable_session(_entry(state="unknown")))
        self.assertFalse(rm.is_reapable_session(_entry(state="")))
        self.assertFalse(rm.is_reapable_session(_entry(native_created_at_ms=None)))

    def test_classify_terminal_idle_dead(self):
        now = 10_000_000
        self.assertEqual(rm.classify_reason(_entry(state="failed"), "exited", False, 3_600_000, now), "terminal")
        idle_entry = _entry(state="running", last_activity_at="1970-01-01T00:00:00Z")
        self.assertEqual(rm.classify_reason(idle_entry, "running", False, 3_600_000, now), "idle")
        recent = _entry(state="running", last_activity_at="1970-01-01T02:46:39Z")
        self.assertIsNone(rm.classify_reason(recent, "running", False, 3_600_000, now))
        unknown_activity = _entry(state="running", last_activity_at=None, started_at=None)
        self.assertIsNone(rm.classify_reason(unknown_activity, "running", False, 3_600_000, now))
        self.assertEqual(rm.classify_reason(_entry(state="running"), "running", True, 3_600_000, now), "dead_pid")

    def test_redact_never_passthrough_secret(self):
        from agent_hub_host.workclaim import redact_for_bus
        red = rm._redact("token=abc sk-12345678901234567890")
        self.assertNotIn("sk-1234", red)


class WatchdogCycleTests(unittest.TestCase):
    def _cycle(self, sessions, native_rows, now_ms=10_000_000, journal=None, capture_text="out"):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if path == "/api/fleet/resources/watchdog":
                return {"config": {"watchdog_interval_s": 300, "idle_minutes": 60, "rss_flag_gb": 4},
                        "sessions": sessions}
            if path == "/api/fleet/resources/evidence":
                return {"saved": True, "evidence_id": payload["id"]}
            if path == "/api/fleet/resources/report":
                return {"reported": True}
            raise AssertionError(path)

        with patch("agent_hub_host.native_sessions.list_sessions", return_value=native_rows), \
             patch("agent_hub_host.native_sessions.qualify_target", side_effect=lambda exe, t, live_only=True: next(
                 r for r in native_rows if r.session_id.lower() == t.session_id.lower())), \
             patch("agent_hub_host.native_sessions.capture_output",
                   return_value=SimpleNamespace(session_id="x", tag="t", mode="tail", text=capture_text,
                                                truncated=False, byte_length=len(capture_text))), \
             patch("agent_hub_host.native_sessions.stop_session",
                   return_value=SimpleNamespace(state="stopped", session_id="x", reason="killed")) as stop_mock, \
             patch.object(rm, "sample_session_tree",
                          return_value={"rss_mb": 100.0, "pss_mb": 50.0, "cpu_seconds": 10.0,
                                        "runtime_s": 100.0, "avg_cpu_cores": 0.1, "current_mb": 100.0}):
            out = rm.run_watchdog_cycle(request, journal_dir=journal, now_ms=now_ms)
        return out, calls, stop_mock

    def test_terminal_reap_evidence_before_stop(self):
        order = []
        native = [_native(phase="exited", last_activity_ms=1_000_000)]
        sessions = [_entry(state="failed")]

        def request(method, path, payload=None):
            order.append(path)
            if path == "/api/fleet/resources/watchdog":
                return {"config": {}, "sessions": sessions}
            if path == "/api/fleet/resources/evidence":
                # Evidence must precede any stop; checked via order list below.
                return {"saved": True}
            if path == "/api/fleet/resources/report":
                self.assertTrue(any(p == "/api/fleet/resources/evidence" for p in order))
                self.assertEqual(payload["reaps"][0]["evidence_id"], payload["reaps"][0]["evidence_id"])
                self.assertTrue(payload["reaps"][0]["stopped"])
                self.assertIn("freed_mb", payload["reaps"][0])
                return {"reported": True}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp, \
             patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
             patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
             patch("agent_hub_host.native_sessions.capture_output",
                   return_value=SimpleNamespace(session_id="x", tag="t", mode="tail", text="tail",
                                                truncated=False, byte_length=4)), \
             patch("agent_hub_host.native_sessions.stop_session",
                   side_effect=lambda exe, target: (order.append("stop"), SimpleNamespace(state="stopped", session_id="x", reason="k"))[1]), \
             patch.object(rm, "sample_session_tree",
                          return_value={"rss_mb": 64.0, "pss_mb": 32.0, "cpu_seconds": 5.0,
                                        "runtime_s": 50.0, "avg_cpu_cores": 0.1, "current_mb": 64.0}):
            out = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
        self.assertEqual(out["reaps"], 1)
        self.assertLess(order.index("/api/fleet/resources/evidence"), order.index("stop"))

    def test_keep_and_unmanaged_never_reaped(self):
        native = [_native(), _native(session_id="22222222-2222-4222-8222-222222222222")]
        sessions = [_entry(keep=True),
                    _entry(session_id="sess-2", native_id="22222222-2222-4222-8222-222222222222",
                           state="failed", managed=False)]
        out, calls, stop_mock = self._cycle(sessions, native)
        stop_mock.assert_not_called()
        self.assertEqual(out["reaps"], 0)

    def test_idle_requires_fresh_activity_recheck(self):
        # Server says idle (old last_activity) but fresh native shows recent output.
        native = [_native(phase="running", last_activity_ms=9_999_000)]
        sessions = [_entry(state="running", last_activity_at="1970-01-01T00:00:00Z")]
        out, calls, stop_mock = self._cycle(sessions, native, now_ms=10_000_000)
        stop_mock.assert_not_called()
        self.assertEqual(out["reaps"], 0)

    def test_unknown_state_no_stop(self):
        native = [_native(phase="unknown")]
        sessions = [_entry(state="failed")]
        out, calls, stop_mock = self._cycle(sessions, native)
        stop_mock.assert_not_called()
        self.assertEqual(out["reaps"], 0)

    def test_incarnation_race_skipped(self):
        native = [_native(created_at_ms=123)]
        sessions = [_entry(state="failed", native_created_at_ms=999_999)]
        out, calls, stop_mock = self._cycle(sessions, native)
        stop_mock.assert_not_called()
        self.assertEqual(out["reaps"], 0)

    def test_auth_failure_best_effort(self):
        def request(method, path, payload=None):
            raise ValueError("HTTP request failed with status 403")
        out = rm.run_watchdog_cycle(request, journal_dir=None, now_ms=10_000_000)
        self.assertFalse(out["ok"])

    def test_watchdog_fetch_failure_best_effort(self):
        with patch("agent_hub_host.native_sessions.list_sessions", side_effect=RuntimeError("x")):
            def request(method, path, payload=None):
                return {"config": {}, "sessions": [_entry(state="failed")]}
            out = rm.run_watchdog_cycle(request, journal_dir=None, now_ms=10_000_000)
            # No native rows -> no samples, but still ok (best effort).
            self.assertTrue(out["ok"])
            self.assertEqual(out["reaps"], 0)

    def test_lifecycle_journal_idempotent_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited")]
            sessions = [_entry(state="failed")]
            out, calls, _ = self._cycle(sessions, native, journal=tmp)
            self.assertEqual(out["reaps"], 1)
            evidence_calls = [c for c in calls if c[1] == "/api/fleet/resources/evidence"]
            report_calls = [c for c in calls if c[1] == "/api/fleet/resources/report"]
            self.assertEqual(len(evidence_calls), 1)
            self.assertEqual(len(report_calls), 1)
            reap = report_calls[0][2]["reaps"][0]
            self.assertTrue(reap["id"].startswith(sessions[0]["session_id"]))
            self.assertEqual(reap["evidence_id"], evidence_calls[0][2]["id"])
            # State persisted with peaks.
            saved = json.loads(Path(tmp, rm.STATE_FILENAME).read_bytes())
            self.assertIn("peaks", saved)


class HeartbeatTests(unittest.TestCase):
    def test_mem_total_present(self):
        from agent_hub_host import api
        hb = api.get_heartbeat_payload()
        self.assertIn("mem_avail_mb", hb)
        # Linux CI hosts expose MemTotal; Windows path sets it too.
        self.assertIn("mem_total_mb", hb)
        self.assertIsInstance(hb["mem_total_mb"], int)
        self.assertGreater(hb["mem_total_mb"], 0)


class LauncherPayloadTests(unittest.TestCase):
    def test_offer_hints_forwarded(self):
        from agent_hub_host import fleet
        src = Path(fleet.__file__).read_text()
        self.assertIn("memory_estimate_mb", src)
        self.assertIn("resource_management", src)
        self.assertIn('payload["memory_estimate_mb"]', src)
        self.assertIn('payload["resource_management"]', src)


class ResourcesCliTests(unittest.TestCase):
    def test_sessions_path_with_filters(self):
        from agent_hub_host import resources
        seen = {}

        def fake_request(method, path, payload=None):
            seen["path"] = path
            return {"sessions": []}
        with patch("agent_hub_host.bus._do_request", side_effect=fake_request):
            resources.fetch_sessions(project="p", engine="codex", kind="goal")
        self.assertIn("/api/fleet/resources/sessions?", seen["path"])
        self.assertIn("project=p", seen["path"])
        self.assertIn("engine=codex", seen["path"])
        self.assertIn("kind=goal", seen["path"])

    def test_overview_and_reaps_paths(self):
        from agent_hub_host import resources
        paths = []

        def fake_request(method, path, payload=None):
            paths.append(path)
            return {}
        with patch("agent_hub_host.bus._do_request", side_effect=fake_request):
            resources.fetch_overview()
            resources.fetch_reaps()
        self.assertIn("/api/fleet/resources", paths)
        self.assertIn("/api/fleet/resources/reaps", paths)

    def test_invalid_filter_refused(self):
        from agent_hub_host import resources
        with self.assertRaises(ValueError):
            resources.fetch_sessions(project="bad/project")


class DispatcherWiringTests(unittest.TestCase):
    def test_once_with_journal_calls_watchdog_best_effort(self):
        from agent_hub_host import fleet
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"version": 1, "workspaces": {"demo": "/repo"},
                   "subscriptions": [{"service": "codex", "account_label": "shared-codex",
                                      "engine": "codex", "admission_argv": ["gate"]}],
                   "journal_dir": tmp}
            calls = []

            def request(method, path, payload=None):
                calls.append(path)
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": []}
                if path == "/api/fleet/resources/report":
                    return {"reported": True}
                if path == "/api/fleet/offers":
                    return {"offers": [], "reconciliation": []}
                return {"offers": []}

            def runner(argv, **kw):
                if argv and argv[0] == "gate":
                    return {"launch_allowed": True, "profile_supported": True,
                            "account_label": "shared-codex",
                            "observed_at": fleet.utcnow()}
                if argv and argv[0] == "aplexer":
                    return []
                return {}

            with patch("agent_hub_host.system.get_system_info",
                       return_value={"capabilities": {}}), \
                 patch("agent_hub_host.api.get_heartbeat_payload",
                       return_value={"type": "heartbeat"}), \
                 patch("agent_hub_host.native_sessions.list_sessions", return_value=[]):
                fleet.Dispatcher(cfg, request, runner).once()
            self.assertIn("/api/fleet/resources/watchdog", calls)

    def test_once_without_journal_skips_watchdog(self):
        from agent_hub_host import fleet
        cfg = {"version": 1, "workspaces": {"demo": "/repo"},
               "subscriptions": [{"service": "codex", "account_label": "shared-codex",
                                  "engine": "codex", "admission_argv": ["gate"]}]}
        calls = []

        def request(method, path, payload=None):
            calls.append(path)
            return {"offers": []}

        def runner(argv, **kw):
            if argv and argv[0] == "gate":
                return {"launch_allowed": True, "profile_supported": True,
                        "account_label": "shared-codex",
                        "observed_at": fleet.utcnow()}
            if argv and argv[0] == "aplexer":
                return []
            return {}

        with patch("agent_hub_host.system.get_system_info",
                   return_value={"capabilities": {}}), \
             patch("agent_hub_host.api.get_heartbeat_payload",
                   return_value={"type": "heartbeat"}):
            fleet.Dispatcher(cfg, request, runner).once()
        self.assertNotIn("/api/fleet/resources/watchdog", calls)


if __name__ == "__main__":
    unittest.main()
