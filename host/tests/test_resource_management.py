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
                keep=False, managed=True, linked_intake_open=False,
                last_activity_at="1970-01-01T00:16:40Z", started_at="1970-01-01T00:15:00Z")
    base.update(kw)
    return base


# /repo is this module's synthetic, registered-unlocked workspace. Real Git
# regression fixtures retain the real parser and lock state.
def setUpModule():
    from agent_hub_host import worktree_guard
    global _synthetic_git_patch
    actual = worktree_guard.is_locked
    _synthetic_git_patch = patch.object(worktree_guard, 'is_locked',
        side_effect=lambda git, path, *args, **kwargs: False if path == '/repo' else actual(git, path, *args, **kwargs))
    _synthetic_git_patch.start()


def tearDownModule():
    _synthetic_git_patch.stop()


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

    def test_interactive_mode_never_reaped(self):
        # RM-PROTECT-5: Hub-linked headless only; interactive goals are
        # sampled at most even when the managed flag were ever loose.
        self.assertFalse(rm.is_reapable_session(_entry(mode="interactive")))
        self.assertTrue(rm.is_reapable_session(_entry(mode="headless")))
        # Mode absent (older server): the tightened managed flag decides.
        self.assertTrue(rm.is_reapable_session(_entry()))

    def test_classify_terminal_idle_dead(self):
        now = 10_000_000
        self.assertEqual(rm.classify_reason(_entry(state="failed"), "exited", False, 3_600_000, now), "terminal")
        idle_entry = _entry(state="running", idle_reap_allowed=True, last_activity_at="1970-01-01T00:00:00Z")
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
                # Exact {saved:true, evidence_id} confirmation is required.
                self.assertTrue(payload["output"])
                return {"saved": True, "evidence_id": payload["id"]}
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
        # Keep/protection is rechecked via an idempotent same-id evidence
        # re-POST immediately before the stop.
        self.assertEqual(order.count("/api/fleet/resources/evidence"), 2)

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
                if path == "/api/fleet/resources/report":
                    return {"reported": True}
                return {"config": {}, "sessions": [_entry(state="failed")]}
            out = rm.run_watchdog_cycle(request, journal_dir=None, now_ms=10_000_000)
            # No native rows -> terminal sessions still emit cached/null final
            # rows (never skipped), reported best-effort.
            self.assertTrue(out["ok"])
            self.assertEqual(out["reaps"], 0)
            self.assertEqual(out["samples"], 1)

    def test_lifecycle_journal_idempotent_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited")]
            sessions = [_entry(state="failed")]
            out, calls, _ = self._cycle(sessions, native, journal=tmp)
            self.assertEqual(out["reaps"], 1)
            evidence_calls = [c for c in calls if c[1] == "/api/fleet/resources/evidence"]
            report_calls = [c for c in calls if c[1] == "/api/fleet/resources/report"]
            # Initial evidence POST plus the idempotent keep/protection
            # recheck immediately before the stop; both carry the same id.
            self.assertEqual(len(evidence_calls), 2)
            self.assertEqual(evidence_calls[0][2]["id"], evidence_calls[1][2]["id"])
            self.assertEqual(len(report_calls), 1)
            reap = report_calls[0][2]["reaps"][0]
            self.assertTrue(reap["id"].startswith(sessions[0]["session_id"]))
            self.assertEqual(reap["evidence_id"], evidence_calls[0][2]["id"])
            # State persisted with peaks; the acked receipt is cleared, the
            # pending intent is gone after the confirmed stop.
            saved = json.loads(Path(tmp, rm.STATE_FILENAME).read_bytes())
            self.assertIn("peaks", saved)
            self.assertEqual(saved.get("pending", {}), {})
            self.assertEqual(saved.get("reported", {}), {})


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


class EvidenceGateTests(unittest.TestCase):
    def _reap_cycle(self, sessions, native_rows, now_ms=10_000_000, journal=None,
                    evidence=None, captures=None, stop=None, samples=None):
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if path == "/api/fleet/resources/watchdog":
                return {"config": {"watchdog_interval_s": 300, "idle_minutes": 60, "rss_flag_gb": 4},
                        "sessions": sessions}
            if path == "/api/fleet/resources/evidence":
                if callable(evidence):
                    return evidence(calls, payload)
                return {"saved": True, "evidence_id": payload["id"]}
            if path == "/api/fleet/resources/report":
                return {"reported": True}
            raise AssertionError(path)

        cap = captures
        if cap is None:
            cap = SimpleNamespace(session_id="x", tag="t", mode="tail", text="tail",
                                  truncated=False, byte_length=4)
        stop_result = stop
        if stop_result is None:
            stop_result = SimpleNamespace(state="stopped", session_id="x", reason="k")
        sample = samples
        if sample is None:
            sample = {"rss_mb": 64.0, "pss_mb": 32.0, "cpu_seconds": 5.0,
                      "runtime_s": 50.0, "avg_cpu_cores": 0.1, "current_mb": 64.0}
        with patch("agent_hub_host.native_sessions.list_sessions", return_value=native_rows), \
             patch("agent_hub_host.native_sessions.qualify_target", side_effect=lambda exe, t, live_only=True: next(
                 r for r in native_rows if r.session_id.lower() == t.session_id.lower())), \
             patch("agent_hub_host.native_sessions.capture_output",
                   side_effect=None if not isinstance(cap, list) else cap,
                   return_value=None if isinstance(cap, list) else cap), \
             patch("agent_hub_host.native_sessions.stop_session",
                   side_effect=None if isinstance(stop_result, BaseException) else None,
                   return_value=None if isinstance(stop_result, BaseException) else stop_result) as stop_mock, \
             patch.object(rm, "sample_session_tree",
                          side_effect=None if isinstance(sample, list) else None,
                          return_value=None if isinstance(sample, list) else sample):
            if isinstance(stop_result, BaseException):
                with patch("agent_hub_host.native_sessions.stop_session",
                           side_effect=stop_result) as stop_mock2:
                    out = rm.run_watchdog_cycle(request, journal_dir=journal, now_ms=now_ms)
                return out, calls, stop_mock2
            out = rm.run_watchdog_cycle(request, journal_dir=journal, now_ms=now_ms)
        return out, calls, stop_mock

    def test_naturally_finished_native_is_sampled_without_reap_or_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            for state in ("running", "exited", "failed", "stopped"):
                with self.subTest(state=state):
                    native = [_native(phase="exited", containment_empty=True)]
                    out, calls, stop = self._reap_cycle([_entry(state=state)], native, journal=tmp)
                    self.assertGreaterEqual(out["samples"], 1)
                    self.assertEqual(out["reaps"], 0)
                    stop.assert_not_called()
                    self.assertFalse(any(path.endswith("/evidence") for _, path, _ in calls))

    def test_server_reap_marker_skips_even_without_local_journal_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, calls, stop = self._reap_cycle([_entry(state="failed", reaped=True)],
                                               [_native()], journal=tmp)
            self.assertEqual(out["reaps"], 0)
            stop.assert_not_called()
            self.assertFalse(any(path.endswith("/evidence") for _, path, _ in calls))

    def test_quiet_terminal_output_is_saved_without_full_idle_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=9_640_000)]
            out, calls, stop_mock = self._reap_cycle(
                [_entry(state="failed")], native, journal=tmp)
            stop_mock.assert_called_once()
            self.assertEqual(out["reaps"], 1)
            evidence = [payload for _, path, payload in calls if path.endswith("/evidence")]
            self.assertTrue(evidence)
            self.assertEqual(evidence[0]["reason"], "terminal")
            self.assertEqual(evidence[0]["output"], "tail")

    def test_terminal_recent_output_refuses_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="running", last_activity_ms=9_940_000)]
            out, calls, stop_mock = self._reap_cycle(
                [_entry(state="failed")], native, journal=tmp)
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)
            self.assertFalse(any(path.endswith("/evidence") for _, path, _ in calls))

    def test_dead_workload_resumed_after_evidence_refuses_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="running", last_activity_ms=9_640_000)]
            with patch.object(rm, "_native_dead", side_effect=[True, True, False]):
                out, calls, stop_mock = self._reap_cycle(
                    [_entry(state="running")], native, journal=tmp)
            self.assertTrue(any(path.endswith("/evidence") for _, path, _ in calls))
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)

    def test_evidence_ack_missing_id_refuses_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            out, _, stop_mock = self._reap_cycle(
                [_entry(state="failed")], native, journal=tmp,
                evidence=lambda calls, payload: {"saved": True})
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)

    def test_evidence_saved_false_refuses_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            out, _, stop_mock = self._reap_cycle(
                [_entry(state="failed")], native, journal=tmp,
                evidence=lambda calls, payload: {"saved": False, "evidence_id": payload["id"]})
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)

    def test_output_race_after_evidence_refuses_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            cap_a = SimpleNamespace(session_id="x", tag="t", mode="tail", text="tail-A",
                                    truncated=False, byte_length=6)
            cap_b = SimpleNamespace(session_id="x", tag="t", mode="tail", text="tail-A-MORE",
                                    truncated=False, byte_length=11)
            out, _, stop_mock = self._reap_cycle(
                [_entry(state="failed")], native, journal=tmp, captures=[cap_a, cap_b])
            # Newer productive output appeared during the evidence window.
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)
            # The pre-evidence intent was journaled; a later quiet cycle retries.
            saved = json.loads(Path(tmp, rm.STATE_FILENAME).read_bytes())
            self.assertTrue(saved.get("pending"))

    def test_keep_set_after_evidence_refuses_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]

            def evidence(calls, payload):
                prior = [c for c in calls if c[1] == "/api/fleet/resources/evidence"]
                if len(prior) > 1:
                    # Server re-POST revalidates keep/protection: now kept.
                    raise RuntimeError("HTTP 400 session_not_reapable")
                return {"saved": True, "evidence_id": payload["id"]}

            out, _, stop_mock = self._reap_cycle(
                [_entry(state="failed")], native, journal=tmp, evidence=evidence)
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)

    def test_idle_uses_full_configured_threshold(self):
        # Entry says idle (61 min) but the FRESH native stamp is 59 min old:
        # the full 60-min threshold refuses (the old 60s rule would have fired).
        with tempfile.TemporaryDirectory() as tmp:
            fresh_ms = 10_000_000 - 59 * 60 * 1000
            native = [_native(phase="running", last_activity_ms=fresh_ms)]
            sessions = [_entry(state="running", last_activity_at="1970-01-01T00:00:00Z")]
            out, _, stop_mock = self._reap_cycle(sessions, native, journal=tmp)
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)

    def test_open_intake_without_fresh_permission_never_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="running", last_activity_ms=1_000_000)]
            sessions = [_entry(state="running", last_activity_at="1970-01-01T00:00:00Z")]
            out, _, stop_mock = self._reap_cycle(sessions, native, journal=tmp)
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)

    def test_idle_quiet_happy_path_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="running", last_activity_ms=1_000_000)]
            sessions = [_entry(state="running", idle_reap_allowed=True, last_activity_at="1970-01-01T00:00:00Z")]
            out, _, stop_mock = self._reap_cycle(sessions, native, journal=tmp)
            stop_mock.assert_called_once()
            self.assertEqual(out["reaps"], 1)

    def test_evidence_output_utf8_byte_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            big = "ü" * 5000  # 10000 UTF-8 bytes, over the 6000 limit.
            cap = SimpleNamespace(session_id="x", tag="t", mode="tail", text=big,
                                  truncated=False, byte_length=len(big.encode("utf-8")))
            out, calls, _ = self._reap_cycle(
                [_entry(state="failed")], native, journal=tmp, captures=cap)
            self.assertEqual(out["reaps"], 1)
            first = next(c for c in calls if c[1] == "/api/fleet/resources/evidence")
            sent = first[2]["output"].encode("utf-8")
            self.assertLessEqual(len(sent), rm.EVIDENCE_MAX_BYTES)
            sent.decode("utf-8")  # Still valid UTF-8 (no split code point).


class JournalTests(unittest.TestCase):
    def test_save_roundtrips_reported_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {"peaks": {"k": {"peak_rss_mb": 10.0}}, "pending": {},
                     "reported": {"rid": {"id": "rid", "session_id": "s", "native_id": "n",
                                          "reason": "terminal", "freed_mb": 5.0, "at": "t",
                                          "evidence_id": "e", "stopped": True}},
                     "last_watchdog_ms": 123}
            self.assertTrue(rm.save_state(tmp, state))
            loaded = rm.load_state(tmp)
            self.assertEqual(loaded["reported"]["rid"]["freed_mb"], 5.0)
            self.assertEqual(loaded["last_watchdog_ms"], 123)

    def test_sweep_drops_only_aborted_stale_intents(self):
        state: dict = {"peaks": {}, "reported": {},
                       "pending": {
                           "old-aborted": {"at_ms": 1_000},
                           "old-oracle": {"at_ms": 1_000, "oracle": {"a": 1}},
                           "fresh": {"at_ms": 100_000_000},
                       }}
        rm._sweep_stale_pending(state, 100_000_000)
        self.assertNotIn("old-aborted", state["pending"])
        self.assertIn("old-oracle", state["pending"])
        self.assertIn("fresh", state["pending"])

    def test_save_oversize_fails_closed_without_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = {"p%d" % i: {"blob": "x" * 3000} for i in range(200)}
            state = {"peaks": {}, "pending": pending, "reported": {}}
            self.assertFalse(rm.save_state(tmp, state))
            path = Path(tmp, rm.STATE_FILENAME)
            if path.exists():
                # Whatever is on disk must still be valid JSON.
                json.loads(path.read_bytes())

    def test_journal_failure_refuses_destructive_stop(self):
        native = [_native(phase="exited", last_activity_ms=1_000_000)]
        sessions = [_entry(state="failed")]

        def request(method, path, payload=None):
            if path == "/api/fleet/resources/watchdog":
                return {"config": {}, "sessions": sessions}
            if path == "/api/fleet/resources/evidence":
                return {"saved": True, "evidence_id": payload["id"]}
            if path == "/api/fleet/resources/report":
                return {"reported": True}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp, \
             patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
             patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
             patch("agent_hub_host.native_sessions.capture_output",
                   return_value=SimpleNamespace(text="t")), \
             patch("agent_hub_host.native_sessions.stop_session") as stop_mock, \
             patch.object(rm, "sample_session_tree", return_value={"rss_mb": 1.0}), \
             patch.object(rm, "save_state", return_value=False):
            out = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
        stop_mock.assert_not_called()
        self.assertEqual(out["reaps"], 0)

    def test_lost_report_replays_until_reported_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed")]
            reports = []
            mode = {"fail": True}

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions if mode["fail"] else []}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    reports.append(payload)
                    if mode["fail"]:
                        raise RuntimeError("transport lost")
                    return {"reported": True}
                raise AssertionError(path)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")), \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "current_mb": 8.0}):
                out1 = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
                self.assertFalse(out1["ok"])
                mid = rm.load_state(tmp)
                self.assertEqual(len(mid["reported"]), 1)
                reap_id = next(iter(mid["reported"]))
                # Second cycle: no candidates, but the unacked receipt replays.
                mode["fail"] = False
                out2 = rm.run_watchdog_cycle(request, journal_dir=tmp,
                                             now_ms=10_000_000 + 301_000)
            self.assertTrue(out2["ok"])
            self.assertEqual(len(reports), 2)
            replayed = [r for r in reports[1]["reaps"] if r["id"] == reap_id]
            self.assertEqual(len(replayed), 1)
            self.assertTrue(replayed[0]["stopped"])
            final = rm.load_state(tmp)
            self.assertEqual(final["reported"], {})


class OracleTests(unittest.TestCase):
    def _oracle(self, **kw):
        base = {"version": 1, "session_id": "11111111-1111-4111-8111-111111111111",
                "tag": "hub-abc", "workspace": "/repo", "created_at_ms": 900_000,
                "engine": "codex", "phase": "exited", "worker_pid": None,
                "workload_pid": None, "processes": None, "containment_empty": True,
                "workload_cgroup": None, "last_activity_ms": 1_000_000}
        base.update(kw)
        return base

    def test_uncertain_stop_confirms_without_second_kill(self):
        from agent_hub_host.native_sessions import _uncertain_with_oracle
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="running", last_activity_ms=1_000_000,
                              containment_empty=False)]
            sessions = [_entry(state="failed")]
            uncertain = _uncertain_with_oracle("kill ok, exit unproven",
                                               self._oracle(), {"signal": "TERM"})

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    return {"reported": True}
                raise AssertionError(path)

            with patch.object(rm, "_native_dead", return_value=False), \
                 patch("agent_hub_host.native_sessions.reconcile_stop_oracle", return_value=(True, "confirmed")), \
                 patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       side_effect=uncertain) as stop_mock, \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "current_mb": 8.0}):
                out = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
            # Read-only reconcile confirmed via the oracle: exactly one kill.
            stop_mock.assert_called_once()
            self.assertEqual(out["reaps"], 1)

    def test_saturated_receipts_flush_before_recovering_an_uncertain_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry, native = _entry(state="failed"), _native()
            state = {"peaks": {}, "pending": {"pending-stop": {
                "oracle": self._oracle(), "session_id": entry["session_id"],
                "native_id": entry["native_id"], "native_created_at_ms": 900_000,
                "reason": "terminal", "evidence_id": "pending-evidence", "at_ms": 1_000_000}},
                "reported": {}}
            for i in range(rm.MAX_REPORTED_ENTRIES):
                state["reported"][str(i)] = dict(id=str(i), session_id="old-" + str(i),
                    native_id="native-" + str(i), reason="terminal", evidence_id="e-" + str(i),
                    at="1970-01-01T00:16:40Z", stopped=True, freed_mb=None)
            self.assertTrue(rm.save_state(tmp, state))
            posted = []
            def request(method, path, payload=None):
                if path.endswith("/watchdog"):
                    return {"config": {}, "sessions": [entry]}
                if path.endswith("/report"):
                    posted.extend(payload["reaps"])
                    return {"reported": True}
                raise AssertionError("Recovery must only report, never capture evidence: " + path)
            with patch("agent_hub_host.native_sessions.list_sessions", return_value=[native]), \
                 patch("agent_hub_host.native_sessions.reconcile_stop_oracle", return_value=(True, "confirmed")), \
                 patch("agent_hub_host.native_sessions.stop_session") as stop, \
                 patch.object(rm, "_native_dead", return_value=False), \
                 patch.object(rm, "sample_session_tree", return_value=None):
                first = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
                self.assertTrue(first["ok"])
                self.assertEqual(len(posted), rm.MAX_REPORTED_ENTRIES)
                saved = rm.load_state(tmp)
                self.assertFalse(saved["reported"])
                self.assertIn("pending-stop", saved["pending"])
                second = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_301_000)
                self.assertTrue(second["ok"])
                self.assertEqual(second["reaps"], 1)
                self.assertEqual(posted[-1]["id"], "pending-stop")
                self.assertFalse(rm.load_state(tmp)["pending"])
                stop.assert_not_called()

    def test_uncertain_denied_never_retries_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="running", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed")]
            from agent_hub_host.native_sessions import _uncertain_with_oracle
            oracle = self._oracle(containment_empty=False)

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    return {"reported": True}
                raise AssertionError(path)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       side_effect=_uncertain_with_oracle("still running", oracle, {})) as stop_mock, \
                 patch("agent_hub_host.native_sessions.reconcile_stop_oracle",
                       side_effect=[(False, "still running"), (True, "cgroup reaped")]), \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "current_mb": 8.0}):
                out1 = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
                self.assertEqual(out1["reaps"], 0)
                out2 = rm.run_watchdog_cycle(request, journal_dir=tmp,
                                             now_ms=10_000_000 + 301_000)
                self.assertEqual(out2["reaps"], 1)
            # One kill total across both cycles: the retry was read-only.
            stop_mock.assert_called_once()


class CgroupScopeTests(unittest.TestCase):
    SCOPE = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-abc.scope"

    def test_native_relative_form_resolves(self):
        self.assertEqual(
            rm._cgroup_path("/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-abc.scope"),
            self.SCOPE)

    def test_shared_slices_rejected(self):
        for bad in ("/sys/fs/cgroup/user.slice",
                    "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service",
                    "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice",
                    "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/run-u1.service",
                    "/sys/fs/cgroup/user.slice/x.slice/session-3.scope",
                    "/sys/fs/cgroup/init.scope",
                    "/sys/fs/cgroup",
                    "user.slice/x.scope",
                    "/sys/fs/cgroup/a/../b.scope",
                    "",
                    None):
            self.assertIsNone(rm._cgroup_path(bad), bad)

    def test_foreign_scope_rejected_by_pid(self):
        scope = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-abc.scope"
        with patch.object(rm, "_pids_in_cgroup", return_value=[9999]):
            self.assertIsNone(rm._cgroup_path(scope, [1234]))
            self.assertEqual(rm._cgroup_path(scope, [9999]), scope)
        with patch.object(rm, "_pids_in_cgroup", return_value=[]):
            # Unreadable/empty membership proves nothing foreign: accepted,
            # sampling still falls back per readable counters.
            self.assertEqual(rm._cgroup_path(scope, [1234]), scope)

    def test_shared_slice_falls_back_to_exact_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            d = proc / "100"
            d.mkdir()
            d.joinpath("stat").write_text(
                "100 (cmd) R 1 1 1 0 -1 0 0 0 0 0 50 50 0 0 20 0 1 0 100 0 0 0")
            d.joinpath("smaps_rollup").write_text(
                "Rss: 10240 kB\nPss: 5120 kB\nAnonymous: 8192 kB\n")
            row = {"workload_cgroup": "/sys/fs/cgroup/user.slice/user-1000.slice",
                   "worker_pid": 100, "workload_pid": None, "created_at_ms": 0}
            out = rm.sample_session_tree(row, 1_000_000, proc_root=str(proc))
            self.assertIsNotNone(out)
            self.assertAlmostEqual(out["rss_mb"], 8.0, delta=0.5)

    def test_sampler_never_uses_current_or_peak_as_rss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg = Path(tmp) / "aplexer-workload-abc.scope"
            cg.mkdir()
            # No memory.stat anon at all: current/peak (page cache) must NOT
            # become RSS; the tree is unknown.
            (cg / "memory.current").write_text("209715200\n")
            (cg / "memory.peak").write_text("314572800\n")
            (cg / "cpu.stat").write_text("usage_usec 1000000\n")
            (cg / "cgroup.procs").write_text("")
            row = {"workload_cgroup": str(cg), "worker_pid": None,
                   "workload_pid": None, "created_at_ms": 0}
            with patch.object(rm, "_cgroup_path", return_value=str(cg)):
                sampled = rm.sample_session_tree(row, 180_000_000, proc_root=tmp)
                self.assertIsNone(sampled["rss_mb"])
                self.assertEqual(sampled["cpu_seconds"], 1.0)


class PeakTests(unittest.TestCase):
    def test_peak_monotonicity_explicit_mapping(self):
        state: dict = {"peaks": {}, "pending": {}, "reported": {}}
        first = rm.update_peaks(state, "s", "n",
                                {"rss_mb": 200.0, "pss_mb": 100.0, "cpu_seconds": 10.0,
                                 "runtime_s": 50.0, "avg_cpu_cores": 0.2, "current_mb": 200.0}, "t0")
        self.assertEqual(first["peak_rss_mb"], 200.0)
        # A smaller sample must NOT reset the stored peak (old code looked up
        # prior "rss_mb" while storing "peak_rss_mb" and lost peaks).
        second = rm.update_peaks(state, "s", "n",
                                 {"rss_mb": 50.0, "pss_mb": None, "cpu_seconds": 30.0,
                                  "runtime_s": 60.0, "avg_cpu_cores": 0.5, "current_mb": 50.0}, "t1")
        self.assertEqual(second["peak_rss_mb"], 200.0)
        self.assertEqual(second["peak_pss_mb"], 100.0)
        self.assertEqual(second["cpu_seconds"], 30.0)
        self.assertEqual(second["runtime_s"], 60.0)

    def test_terminal_runtime_frozen_not_growing(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed", finished_at="1970-01-01T00:20:00Z",
                               started_at="1970-01-01T00:15:00Z")]
            seen = []

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    seen.append(payload)
                    return {"reported": True}
                raise AssertionError(path)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")), \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "cpu_seconds": 9.0,
                                            "runtime_s": 9999.0, "current_mb": 8.0}):
                rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
                # A later heartbeat must not advance the frozen runtime.
                rm.run_watchdog_cycle(request, journal_dir=None,
                                      now_ms=10_000_000 + 100_000)
            # finished 00:20 - started 00:15 = 300s, frozen on both heartbeats.
            self.assertEqual(seen[0]["samples"][0]["runtime_s"], 300.0)
            self.assertEqual(seen[1]["samples"][0]["runtime_s"], 300.0)

    def test_terminal_without_native_emits_final_row(self):
        def request(method, path, payload=None):
            if path == "/api/fleet/resources/watchdog":
                return {"config": {}, "sessions": [_entry(state="failed")]}
            if path == "/api/fleet/resources/report":
                return {"reported": True}
            raise AssertionError(path)

        with patch("agent_hub_host.native_sessions.list_sessions", return_value=[]):
            out = rm.run_watchdog_cycle(request, journal_dir=None, now_ms=10_000_000)
        self.assertTrue(out["ok"])
        self.assertEqual(out["samples"], 1)
        self.assertEqual(out["reaps"], 0)

    def test_unknown_cpu_stays_null_never_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg = Path(tmp) / "aplexer-workload-abc.scope"
            cg.mkdir()
            (cg / "memory.stat").write_text("anon 104857600\n")
            (cg / "memory.current").write_text("104857600\n")
            (cg / "cgroup.procs").write_text("")
            row = {"workload_cgroup": str(cg), "worker_pid": None,
                   "workload_pid": None, "created_at_ms": 0}
            with patch.object(rm, "_cgroup_path", return_value=str(cg)):
                out = rm.sample_session_tree(row, 180_000_000, proc_root=tmp)
            self.assertIsNotNone(out)
            self.assertAlmostEqual(out["rss_mb"], 100.0, delta=1.0)
            self.assertIsNone(out["cpu_seconds"])
            self.assertIsNone(out["avg_cpu_cores"])
            self.assertIsNone(out["pss_mb"])
            reported = rm._sample_row("s", "n", {"peak_rss_mb": out["rss_mb"],
                                                 "peak_pss_mb": None, "cpu_seconds": None,
                                                 "runtime_s": None, "avg_cpu_cores": None}, "t")
            self.assertNotIn("cpu_seconds", reported)
            self.assertNotIn("peak_pss_mb", reported)


class BatchCadenceTests(unittest.TestCase):
    def test_destructive_pass_gated_to_configured_cadence(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed")]
            counts = []

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    return {"reported": True}
                raise AssertionError(path)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")), \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "current_mb": 8.0}):
                counts.append(rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)["reaps"])
                # Same heartbeat immediately after: sampling continues, no stops.
                mid = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000 + 60_000)
                counts.append(mid["reaps"])
                self.assertGreaterEqual(mid["samples"], 1)
                # After the 300s cadence: destructive pass runs again.
                counts.append(rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000 + 301_000)["reaps"])
            self.assertEqual(counts, [1, 0, 0])  # Same incarnation stays settled after ACK/restart.

    def test_full_watchdog_catalog_reaches_reporting_past_old_500_slice(self):
        catalog = [_entry(session_id="s%d" % i, native_id="n%d" % i, state="failed")
                   for i in range(884)]
        posted = []
        def request(method, path, payload=None):
            if path == "/api/fleet/resources/watchdog":
                return {"config": {}, "sessions": catalog}
            if path == "/api/fleet/resources/report":
                posted.extend(payload["samples"])
                return {"reported": True}
            raise AssertionError(path)
        with patch("agent_hub_host.native_sessions.list_sessions", return_value=[]), \
             patch("agent_hub_host.native_sessions.stop_session") as stop:
            out = rm.run_watchdog_cycle(request, journal_dir=None, now_ms=10_000_000)
        self.assertTrue(out["ok"])
        self.assertEqual(out["samples"], len(catalog))
        self.assertEqual([r["session_id"] for r in posted], [r["session_id"] for r in catalog])
        self.assertEqual(out["reaps"], 0)
        stop.assert_not_called()

    def test_all_observations_and_full_receipt_journal_reach_bounded_posts(self):
        # Real host catalogs exceed one body. A per-POST cap cannot silently
        # truncate the catalog or emit a receipt batch the API rejects (>100).
        samples = [{"session_id": str(i)} for i in range(884)]
        reaps = [{"id": str(i)} for i in range(rm.MAX_REPORTED_ENTRIES)]
        posts = rm._split_report_posts(samples, reaps)
        self.assertEqual([s for p in posts for s in p["samples"]], samples)
        self.assertEqual([r for p in posts for r in p["reaps"]], reaps)
        for post in posts:
            self.assertLessEqual(len(post["samples"]), 200)
            self.assertLessEqual(len(post["reaps"]), 50)
            self.assertLess(len(json.dumps(post).encode()), rm.REPORT_BYTE_BUDGET)

    def test_report_batches_stay_well_under_body_cap(self):
        fat = [{"session_id": "s%d" % i, "native_id": "n%d" % i, "observed_at": "t",
                "peak_rss_mb": 1.5, "pad": "x" * 500} for i in range(200)]
        reaps = [{"id": "r%d" % i, "session_id": "s", "native_id": "n", "reason": "idle",
                  "freed_mb": 1.0, "at": "t", "evidence_id": "e", "stopped": True} for i in range(3)]
        posts = rm._split_report_posts(fat, reaps)
        self.assertGreater(len(posts), 1)
        total = 0
        for post in posts:
            size = len(json.dumps(post).encode("utf-8"))
            self.assertLess(size, rm.REPORT_BYTE_BUDGET)
            self.assertLessEqual(len(post["samples"]), 200)
            total += len(post["samples"])
        self.assertEqual(total, 200)
        self.assertEqual(sum(len(p["reaps"]) for p in posts), 3)

    def test_config_clamped_to_server_bounds(self):
        cfg = rm.normalize_config({"watchdog_interval_s": 5, "idle_minutes": 1, "rss_flag_gb": 0.01})
        self.assertEqual(cfg["watchdog_interval_s"], 300)
        self.assertEqual(cfg["idle_minutes"], 10)
        self.assertEqual(cfg["rss_flag_gb"], 0.125)
        cfg = rm.normalize_config({"watchdog_interval_s": 3600, "idle_minutes": 99999, "rss_flag_gb": 99999})
        self.assertEqual(cfg["watchdog_interval_s"], 600)
        self.assertEqual(cfg["idle_minutes"], 10080)
        self.assertEqual(cfg["rss_flag_gb"], 1024)
        self.assertEqual(rm.watchdog_interval_ms({"watchdog_interval_s": 1}), 300_000)

    def test_truncate_utf8_bytes_never_splits_codepoint(self):
        out = rm._truncate_utf8_bytes("é" * 4000, 6000)
        raw = out.encode("utf-8")
        self.assertEqual(len(raw), 6000)
        raw.decode("utf-8")
        self.assertEqual(rm._truncate_utf8_bytes("a" * 7000, 6000), "a" * 6000)
        self.assertEqual(rm._truncate_utf8_bytes("abc", 6000), "abc")

    def test_redact_fallback_never_returns_raw(self):
        secret = "token=abc sk-12345678901234567890"
        with patch("agent_hub_host.workclaim.redact_for_bus", side_effect=RuntimeError("down")):
            red = rm._redact(secret)
        self.assertNotIn("sk-1234", red)
        self.assertNotIn("token=abc", red)

    def test_freed_unknown_reports_null_never_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed")]
            seen = []

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    seen.append(payload)
                    return {"reported": True}
                raise AssertionError(path)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")) as stop_mock, \
                 patch.object(rm, "sample_session_tree", return_value=None):
                out = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
            # Stop confirmed with unproven release: reap reported with
            # freed_mb null (server stores unknown), never a fabricated 0.
            stop_mock.assert_called_once()
            self.assertEqual(out["reaps"], 1)
            self.assertIsNone(seen[0]["reaps"][0]["freed_mb"])
            self.assertTrue(seen[0]["reaps"][0]["stopped"])
            # Acked: nothing retained.
            self.assertEqual(rm.load_state(tmp)["reported"], {})

    def test_per_attempt_evidence_ids_and_incarnation_reap_id(self):
        import re
        self.assertEqual(
            rm._evidence_id("s", "idle", 900_000, 1_000_000, "ab12"),
            rm._evidence_id("s", "idle", 900_000, 1_000_000, "ab12"))
        self.assertNotEqual(
            rm._evidence_id("s", "idle", 900_000, 1_000_000, "ab12"),
            rm._evidence_id("s", "idle", 900_001, 1_000_000, "ab12"))
        self.assertNotEqual(
            rm._evidence_id("s", "idle", 900_000, 1_000_000, "ab12"),
            rm._evidence_id("s", "idle", 900_000, 1_000_001, "ab12"))
        self.assertNotEqual(
            rm._evidence_id("s", "idle", 900_000, 1_000_000, "ab12"),
            rm._evidence_id("s", "idle", 900_000, 1_000_000, "cd34"))
        reap = rm._reap_id("sess-1", "terminal", 900_000, "ab12cd34")
        self.assertIn("900000", reap)  # Native incarnation included.
        self.assertTrue(re.match(r"^[\w:-]{1,128}$", reap))

    def test_acknowledged_stop_never_reaps_same_incarnation_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed")]
            evidence_ids = []

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    evidence_ids.append(payload["id"])
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    return {"reported": True}
                raise AssertionError(path)

            def cap_old(exe, target, limit_bytes=1, mode="tail"):
                return SimpleNamespace(session_id="x", tag="t", mode="tail",
                                        text="old", truncated=False, byte_length=3)

            def cap_new(exe, target, limit_bytes=1, mode="tail"):
                return SimpleNamespace(session_id="x", tag="t", mode="tail",
                                        text="new-output", truncated=False, byte_length=10)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output", side_effect=cap_old), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")) as stop_mock, \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "current_mb": 8.0}):
                # Cycle 1: snapshot "old", recheck "old" (stable) -> stops.
                out1 = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
            self.assertEqual(out1["reaps"], 1)
            first_id = evidence_ids[-1]
            # A stale server/native list after ACK cannot trigger another stop,
            # even after cadence or when the retained output changes.
            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output", side_effect=cap_new), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")), \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "current_mb": 8.0}):
                rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000 + 301_000)
            self.assertEqual(evidence_ids, [first_id, first_id])
            saved = json.loads(Path(tmp, rm.STATE_FILENAME).read_bytes())
            self.assertFalse(saved["reported"])
            self.assertEqual(saved["peaks"][rm._peak_key("sess-1", native[0].session_id)]["reaped_created_at_ms"], 900_000)
            _ = stop_mock

    def test_reap_at_uses_post_stop_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed")]
            seen = []

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    seen.append(payload)
                    return {"reported": True}
                raise AssertionError(path)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")), \
                 patch.object(rm, "sample_session_tree",
                              return_value={"rss_mb": 8.0, "current_mb": 8.0}):
                out = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000,
                                            clock=lambda: 20_000_000)
            self.assertEqual(out["reaps"], 1)
            # 20_000_000 ms = 1970-01-01T05:33:20Z, not the stale cycle start.
            self.assertEqual(seen[0]["reaps"][0]["at"], "1970-01-01T05:33:20Z")

    def test_current_rss_reported_each_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="running", last_activity_ms=1_000_000)]
            sessions = [_entry(state="running", last_activity_at="1970-01-01T00:00:00Z",
                               started_at="1970-01-01T00:15:00Z")]
            seen = []

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/report":
                    seen.append(payload)
                    return {"reported": True}
                raise AssertionError(path)

            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native):
                with patch.object(rm, "sample_session_tree",
                                  return_value={"rss_mb": 42.0, "pss_mb": 20.0,
                                                "cpu_seconds": 3.0, "runtime_s": 100.0,
                                                "avg_cpu_cores": 0.03, "current_mb": 42.0}):
                    out = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
            self.assertTrue(out["ok"])
            # Fresh current anon RSS credited against the ramp (not the peak).
            self.assertEqual(seen[0]["samples"][0]["current_rss_mb"], 42.0)

    def test_freed_is_measured_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = [_native(phase="exited", last_activity_ms=1_000_000)]
            sessions = [_entry(state="failed")]
            seen = []

            def request(method, path, payload=None):
                if path == "/api/fleet/resources/watchdog":
                    return {"config": {}, "sessions": sessions}
                if path == "/api/fleet/resources/evidence":
                    return {"saved": True, "evidence_id": payload["id"]}
                if path == "/api/fleet/resources/report":
                    seen.append(payload)
                    return {"reported": True}
                raise AssertionError(path)

            before = {"rss_mb": 90.0, "current_mb": 100.0}
            with patch("agent_hub_host.native_sessions.list_sessions", return_value=native), \
                 patch("agent_hub_host.native_sessions.qualify_target", return_value=native[0]), \
                 patch("agent_hub_host.native_sessions.capture_output",
                       return_value=SimpleNamespace(text="t")), \
                 patch("agent_hub_host.native_sessions.stop_session",
                       return_value=SimpleNamespace(state="stopped")), \
                 patch.object(rm, "sample_session_tree",
                              side_effect=[dict(before), dict(before), None]):
                out = rm.run_watchdog_cycle(request, journal_dir=tmp, now_ms=10_000_000)
            self.assertEqual(out["reaps"], 1)
            # Tree gone after confirmed death: full measured release freed.
            self.assertEqual(seen[0]["reaps"][0]["freed_mb"], 100.0)


class ResourcesCliRenderTests(unittest.TestCase):
    def test_sessions_text_shows_full_ledger(self):
        from agent_hub_host import resources
        data = {"sessions": [{"session_id": "s1", "project": "p", "engine": "codex",
                              "model": "m", "tier": "quick", "kind": "goal",
                              "state": "running", "finished_at": None,
                              "peak_rss_mb": 128.0, "peak_pss_mb": 64.0,
                              "cpu_seconds": 12.5, "avg_cpu_cores": 0.25,
                              "runtime_s": 90.0, "rss_flagged": True}]}
        text = resources.render_text(data, "sessions")
        for want in ("peak_rss=128 MiB", "peak_pss=64 MiB", "cpu=12.5s", "avg_cpu=0.25",
                     "runtime=1m30s", "project=p", "engine=codex", "model=m",
                     "tier=quick", "kind=goal", "state=running", "FLAGGED"):
            self.assertIn(want, text)

    def test_sessions_text_final_state(self):
        from agent_hub_host import resources
        data = {"sessions": [{"session_id": "s1", "state": "stopped",
                              "finished_at": "2026-10-26T00:00:00Z"}]}
        text = resources.render_text(data, "sessions")
        self.assertIn("finished=2026-10-26T00:00:00Z", text)
        self.assertIn("peak_rss=unknown", text)

    def test_reaps_text_shows_actor_intake(self):
        from agent_hub_host import resources
        data = {"reaps": [{"session_id": "s1", "reason": "idle", "freed_mb": 200.0,
                           "at": "2026-10-26T00:00:00Z", "actor": "machine:h1",
                           "intake_id": "int-1", "evidence_id": "e1"}]}
        text = resources.render_text(data, "reaps")
        for want in ("actor=machine:h1", "intake=int-1", "at=2026-10-26T00:00:00Z",
                     "reason=idle", "freed=200 MiB", "evidence=e1"):
            self.assertIn(want, text)

    def test_project_filter_accepts_absolute_path(self):
        from agent_hub_host import resources
        seen = {}

        def fake_request(method, path, payload=None):
            seen["path"] = path
            return {"sessions": []}
        with patch("agent_hub_host.bus._do_request", side_effect=fake_request):
            resources.fetch_sessions(project="/home/alexey/git")
        self.assertIn("project=%2Fhome%2Falexey%2Fgit", seen["path"])

    def test_project_filter_rejects_traversal(self):
        from agent_hub_host import resources
        for bad in ("/a/../b", "/a//b", "bad/project", "/trailing/", "/"):
            with self.assertRaises(ValueError, msg=bad):
                resources.fetch_sessions(project=bad)

    def test_task_kind_filter(self):
        from agent_hub_host import resources
        seen = {}

        def fake_request(method, path, payload=None):
            seen["path"] = path
            return {"sessions": []}
        with patch("agent_hub_host.bus._do_request", side_effect=fake_request):
            resources.fetch_sessions(task_kind="review")
        self.assertIn("kind=review", seen["path"])


class CpuThrottleTests(unittest.TestCase):
    """Reversible watchdog CPU throttle (intake f31b678f, AC2/AC3)."""

    SESSION = "sess-thr"
    NATIVE = "22222222-2222-4222-8222-222222222222"

    def _entry(self, **kw):
        base = _entry(session_id=self.SESSION, native_id=self.NATIVE,
                      fleet_launch_id="launch-thr", state="running")
        base.update(kw)
        return base

    def _native(self, **kw):
        base = dict(session_id=self.NATIVE, tag="hub-thr", workspace="/repo",
                    engine="codex", profile=None, phase="running",
                    last_activity_ms=1_000_000, created_at_ms=900_000, cwd=None,
                    containment_empty=False, workload_cgroup=None, worker_pid=None,
                    workload_pid=None, processes=None, agent_state_at_ms=None,
                    headless_exec=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def _cycle(self, now_ms, sample, journal, entries=None, natives=None,
               config=None, report_responses=None):
        """One watchdog cycle with a caller-supplied sampler result."""
        calls = {"reports": [], "stops": 0}
        cfg = {"watchdog_interval_s": 300, "idle_minutes": 60, "rss_flag_gb": 4,
               "cpu_throttle_cores": 2.0, "cpu_throttle_sustain_s": 30}
        if config:
            cfg.update(config)

        def request(method, path, payload=None):
            if path == "/api/fleet/resources/watchdog":
                return {"config": cfg, "sessions": entries if entries is not None else [self._entry()]}
            if path == "/api/fleet/resources/report":
                calls["reports"].append(payload)
                if report_responses:
                    response = report_responses.pop(0)
                    if isinstance(response, Exception):
                        raise response
                    return response
                return {"reported": True}
            raise AssertionError(path)

        with patch("agent_hub_host.native_sessions.list_sessions",
                   return_value=natives if natives is not None else [self._native()]), \
             patch("agent_hub_host.native_sessions.qualify_target",
                   side_effect=lambda exe, t, live_only=True: (natives or [self._native()])[0]), \
             patch("agent_hub_host.native_sessions.stop_session") as stop_mock, \
             patch.object(rm, "sample_session_tree", return_value=sample):
            out = rm.run_watchdog_cycle(request, journal_dir=journal, now_ms=now_ms)
            calls["stops"] = stop_mock.call_count
        return out, calls

    def _cgroup(self, tmp, cpu_max="max 100000"):
        cg = Path(tmp) / "aplexer-workload-thr.scope"
        cg.mkdir(exist_ok=True)
        (cg / "cpu.max").write_text(cpu_max + "\n")
        return cg

    @staticmethod
    def _sample(cg, cpu_seconds, previous=None):
        """Sampler result whose cgroup delta yields >2 cores between samples."""
        out = {"rss_mb": 100.0, "pss_mb": 50.0, "cpu_seconds": cpu_seconds,
               "runtime_s": 1000.0, "avg_cpu_cores": 0.1, "current_mb": 100.0,
               "cpu_identity": "cgroup:%s:4711" % cg,
               "cgroup_path": str(cg)}
        if previous is not None:
            out["cpu_identity"] = previous["cpu_identity"]
        return out

    def test_throttle_applies_after_sustained_over_cap_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            over = self._sample(cg, 100.0)
            out1, calls1 = self._cycle(10_000_000, over, journal)
            self.assertEqual(calls1["stops"], 0)
            self.assertFalse(any(p.get("throttles") for p in calls1["reports"]))
            # 31s later the delta is 99 cores-seconds / 31s ~ 3.2 cores > cap 2.
            over2 = self._sample(cg, 199.0, previous=over)
            out2, calls2 = self._cycle(10_031_000, over2, journal)
            self.assertEqual(calls2["stops"], 0)  # Never kills.
            receipts = [t for p in calls2["reports"] for t in p.get("throttles", [])]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["action"], "throttled")
            self.assertEqual(receipts[0]["session_id"], self.SESSION)
            self.assertEqual(receipts[0]["cpu_max"], "2000000 100000")
            self.assertEqual(receipts[0]["prior_cpu_max"], "max 100000")
            self.assertEqual((cg / "cpu.max").read_text().strip(), "2000000 100000")
            self.assertTrue(out2.get("ok"))

    def test_throttle_lifts_when_usage_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            self._cycle(10_000_000, self._sample(cg, 100.0), journal)
            self._cycle(10_031_000, self._sample(cg, 199.0), journal)
            self.assertEqual((cg / "cpu.max").read_text().strip(), "2000000 100000")
            idle = self._sample(cg, 199.5)
            out3, calls3 = self._cycle(10_062_000, idle, journal)
            receipts = [t for p in calls3["reports"] for t in p.get("throttles", [])]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["action"], "released")
            self.assertEqual(receipts[0]["outcome"], "restored")
            self.assertEqual(receipts[0]["cpu_max"], "max 100000")
            self.assertEqual((cg / "cpu.max").read_text().strip(), "max 100000")

    def test_throttle_persists_at_cap_without_flapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            self._cycle(10_000_000, self._sample(cg, 100.0), journal)
            self._cycle(10_031_000, self._sample(cg, 199.0), journal)
            # Throttled ceiling over a 31s interval: dc=62 -> 2.0 cores,
            # NOT below the 80% release line (1.6).
            at_cap = self._sample(cg, 261.0)
            _, calls = self._cycle(10_062_000, at_cap, journal)
            receipts = [t for p in calls["reports"] for t in p.get("throttles", [])]
            self.assertEqual(receipts, [])
            self.assertEqual((cg / "cpu.max").read_text().strip(), "2000000 100000")

    def test_throttle_never_applies_to_keep_or_unreadable_cpu_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            keep = self._entry(keep=True)
            self._cycle(10_000_000, self._sample(cg, 100.0), journal, entries=[keep])
            _, calls = self._cycle(10_031_000, self._sample(cg, 199.0), journal, entries=[keep])
            receipts = [t for p in calls["reports"] for t in p.get("throttles", [])]
            self.assertEqual(receipts, [])
            self.assertEqual((cg / "cpu.max").read_text().strip(), "max 100000")
            # Unreadable cpu.max fails safe: no throttle, no crash.
            (cg / "cpu.max").unlink()
            out, calls2 = self._cycle(10_062_000, self._sample(cg, 298.0), journal)
            self.assertTrue(out.get("ok"))
            self.assertEqual([t for p in calls2["reports"] for t in p.get("throttles", [])], [])

    def test_throttle_never_applies_to_unmanaged_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            unmanaged = self._entry(managed=False)
            self._cycle(10_000_000, self._sample(cg, 100.0), journal, entries=[unmanaged])
            _, calls = self._cycle(10_031_000, self._sample(cg, 199.0), journal, entries=[unmanaged])
            receipts = [t for p in calls["reports"] for t in p.get("throttles", [])]
            self.assertEqual(receipts, [])
            self.assertEqual((cg / "cpu.max").read_text().strip(), "max 100000")

    def test_keep_protection_lifts_throttle_immediately_at_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            self._cycle(10_000_000, self._sample(cg, 100.0), journal)
            self._cycle(10_031_000, self._sample(cg, 199.0), journal)
            self.assertEqual((cg / "cpu.max").read_text().strip(), "2000000 100000")
            # Protection gained while pinned at the throttled ceiling: the
            # cap is lifted at once, not held until usage drops.
            protected = self._entry(keep=True)
            _, calls = self._cycle(10_062_000, self._sample(cg, 261.0), journal, entries=[protected])
            receipts = [t for p in calls["reports"] for t in p.get("throttles", [])]
            self.assertEqual([r["action"] for r in receipts], ["released"])
            self.assertEqual((cg / "cpu.max").read_text().strip(), "max 100000")
            # And a protected session is never re-throttled.
            _, calls2 = self._cycle(10_093_000, self._sample(cg, 323.0), journal, entries=[protected])
            self.assertEqual([t for p in calls2["reports"] for t in p.get("throttles", [])], [])

    def test_terminal_session_releases_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            self._cycle(10_000_000, self._sample(cg, 100.0), journal)
            self._cycle(10_031_000, self._sample(cg, 199.0), journal)
            self.assertEqual((cg / "cpu.max").read_text().strip(), "2000000 100000")
            terminal = self._entry(state="failed")
            done = self._sample(cg, 199.2)
            _, calls = self._cycle(10_062_000, done, journal, entries=[terminal])
            receipts = [t for p in calls["reports"] for t in p.get("throttles", [])]
            self.assertEqual([r["action"] for r in receipts], ["released"])
            self.assertEqual((cg / "cpu.max").read_text().strip(), "max 100000")

    def test_unacked_throttle_receipt_replays_until_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal")
            Path(journal).mkdir(mode=0o700)
            cg = self._cgroup(tmp)
            self._cycle(10_000_000, self._sample(cg, 100.0), journal)
            # The apply cycle's report transport fails: the receipt must
            # survive durably even though the cpu.max write happened.
            self._cycle(10_031_000, self._sample(cg, 199.0), journal,
                        report_responses=[RuntimeError("transport down")])
            state = rm.load_state(journal)
            self.assertTrue(state["throttle_reports"])
            # Next cycle replays the same idempotent receipt; ACK clears it.
            # Usage stays at the throttled ceiling (2.0 cores), so the only
            # reported receipt is the replayed apply.
            _, calls = self._cycle(10_062_000, self._sample(cg, 261.0), journal)
            replayed = [t for p in calls["reports"] for t in p.get("throttles", [])]
            self.assertEqual([r["action"] for r in replayed], ["throttled"])
            state = rm.load_state(journal)
            self.assertEqual(state["throttle_reports"], {})

    """AC2: the watchdog never reaps locked worktrees or open-intake sessions."""

    def test_unknown_intake_and_git_lock_authority_never_reap(self):
        native = [_native(phase='exited', last_activity_ms=1_000_000)]
        for unknown in (None, 'false', 0):
            with tempfile.TemporaryDirectory() as tmp:
                result, _, stop = WatchdogCycleTests()._cycle([_entry(state='failed', linked_intake_open=unknown)], native, journal=tmp)
            stop.assert_not_called()
            self.assertEqual(result['reaps'], 0)
        for unknown in (None, True):
            with tempfile.TemporaryDirectory() as tmp, patch('agent_hub_host.worktree_guard.is_locked', return_value=unknown):
                result, _, stop = WatchdogCycleTests()._cycle([_entry(state='failed')], native, journal=tmp)
            stop.assert_not_called()
            self.assertEqual(result['reaps'], 0)

    def test_open_intake_entry_never_reaped(self):
        native = [_native(phase="running", last_activity_ms=1_000_000)]
        sessions = [_entry(state="running", last_activity_at="1970-01-01T00:00:00Z",
                           linked_intake_open=True)]
        with tempfile.TemporaryDirectory() as tmp:
            out, calls, stop_mock = WatchdogCycleTests()._cycle(sessions, native, journal=tmp)
        stop_mock.assert_not_called()
        self.assertEqual(out["reaps"], 0)
        self.assertGreaterEqual(out["skipped"], 1)

    def test_reap_one_refuses_open_intake(self):
        native = _native()
        entry = _entry(linked_intake_open=True)
        state = {"peaks": {}, "pending": {}, "reported": {}}
        self.assertIsNone(rm._reap_one(lambda m, p, b=None: {}, "aplexer", entry,
                                        native, "idle", state, 10_000_000, idle_ms=3_600_000))

    def test_locked_worktree_skipped(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin")
            os.mkdir(origin)
            subprocess.run(["git", "init", "-q", origin], check=False)
            subprocess.run(["git", "-C", origin, "config", "user.email", "t@t"], check=False)
            subprocess.run(["git", "-C", origin, "config", "user.name", "t"], check=False)
            Path(origin, "f").write_text("x")
            subprocess.run(["git", "-C", origin, "add", "f"], check=False)
            subprocess.run(["git", "-C", origin, "commit", "-qm", "i"], check=False)
            wt = os.path.join(tmp, "wt")
            subprocess.run(["git", "-C", origin, "worktree", "add", wt, "-b", "b1"],
                           capture_output=True, check=False)
            subprocess.run(["git", "worktree", "lock", "--reason", "hub-session test", wt],
                           capture_output=True, check=False, cwd=wt)
            native = [_native(workspace=wt)]
            sessions = [_entry(workspace=wt, state="failed")]
            out, calls, stop_mock = WatchdogCycleTests()._cycle(sessions, native, journal=tmp)
            stop_mock.assert_not_called()
            self.assertEqual(out["reaps"], 0)


if __name__ == "__main__":
    unittest.main()
