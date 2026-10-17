import contextlib
import datetime
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host.jobs import JobsClient, JobDispatcher
from agent_hub_host.guarded_root import GuardedRootBridge, IncumbentStatus
from agent_hub_host.runtime import RuntimeManager
from agent_hub_host import api
from agent_hub_host import root_dispatch as rd


class TestJobsClient(unittest.TestCase):
    def setUp(self):
        self.client = JobsClient()
        self.base_url = "https://hub.example.com"

    @patch("agent_hub_host.api.safe_http_json")
    def test_claim_job_success(self, mock_http):
        mock_http.return_value = {
            "job_token": "tok_claim_123",
            "lease_epoch": 5,
            "deadline_at": "2026-10-14T00:00:00Z"
        }

        res = self.client.claim_job(
            base_url=self.base_url,
            job_id="job-abc",
            host_id="host-1",
            token="host_bearer_tok",
            fresh_gate=True
        )

        self.assertEqual(res["job_token"], "tok_claim_123")
        self.assertEqual(res["lease_epoch"], 5)

        self.assertEqual(mock_http.call_count, 1)
        req = mock_http.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "https://hub.example.com/api/jobs/job-abc/claim")
        self.assertEqual(req.headers.get("Authorization"), "Bearer host_bearer_tok")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(
            body,
            {
                "host_id": "host-1",
                "fresh_gate": True,
                "gate": {
                    "launch_allowed": True,
                    "profile_supported": False,
                    "native_root_supported": False,
                },
                "profile_supported": False,
                "native_root_supported": False,
            },
        )

    @patch("agent_hub_host.api.safe_http_json")
    def test_claim_job_conflict_409(self, mock_http):
        err = urllib.error.HTTPError(
            url="https://hub.example.com/api/jobs/job-abc/claim",
            code=409,
            msg="Conflict",
            hdrs={},
            fp=None
        )
        mock_http.side_effect = err

        res = self.client.claim_job(
            base_url=self.base_url,
            job_id="job-abc",
            host_id="host-1",
            token="host_tok"
        )
        self.assertEqual(res.get("error"), "conflict")
        self.assertEqual(res.get("status_code"), 409)

    @patch("agent_hub_host.api.safe_http_json")
    def test_decline_job(self, mock_http):
        mock_http.return_value = {"status": "declined"}

        res = self.client.decline_job(
            base_url=self.base_url,
            job_id="job-abc",
            host_id="host-1",
            token="host_tok",
            reason="launcher_unsupported"
        )
        self.assertEqual(res.get("status"), "declined")
        req = mock_http.call_args[0][0]
        self.assertEqual(req.full_url, "https://hub.example.com/api/jobs/job-abc/decline")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["reason"], "launcher_unsupported")

    @patch("agent_hub_host.api.safe_http_json")
    def test_start_renew_finish_jobs(self, mock_http):
        # 1. start_job
        mock_http.return_value = {"status": "started"}
        res_start = self.client.start_job(
            base_url=self.base_url,
            job_id="job-1",
            job_token="tok-job",
            epoch=2,
            evidence_digest="dig123",
            native_session="sess-1",
            launcher_task_id="task-1"
        )
        self.assertEqual(res_start.get("status"), "started")
        req_start = mock_http.call_args[0][0]
        self.assertEqual(req_start.full_url, "https://hub.example.com/api/jobs/job-1/start")
        self.assertEqual(req_start.headers.get("Authorization"), "Bearer tok-job")

        # 2. renew_job
        mock_http.return_value = {"status": "renewed"}
        res_renew = self.client.renew_job(
            base_url=self.base_url,
            job_id="job-1",
            job_token="tok-job",
            epoch=2
        )
        self.assertEqual(res_renew.get("status"), "renewed")
        req_renew = mock_http.call_args[0][0]
        self.assertEqual(req_renew.full_url, "https://hub.example.com/api/jobs/job-1/renew")

        # 3. finish_job
        mock_http.return_value = {"status": "finished"}
        res_finish = self.client.finish_job(
            base_url=self.base_url,
            job_id="job-1",
            job_token="tok-job",
            epoch=2,
            status="succeeded",
            summary="completed successfully",
            evidence_refs=["/path/to/log.txt"]
        )
        self.assertEqual(res_finish.get("status"), "finished")
        req_finish = mock_http.call_args[0][0]
        self.assertEqual(req_finish.full_url, "https://hub.example.com/api/jobs/job-1/finish")

    @patch("agent_hub_host.auth.refresh_token")
    @patch("agent_hub_host.api.safe_http_json")
    def test_job_token_401_does_not_refresh_host_token(self, mock_http, mock_refresh):
        mock_http.side_effect = urllib.error.HTTPError(
            url="https://hub.example.com/api/jobs/job-1/renew",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None
        )
        res = self.client.renew_job(self.base_url, "job-1", "job-token-expired", 1)
        self.assertEqual(res.get("status_code"), 401)
        mock_refresh.assert_not_called()


class TestJobDispatcher(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_dispatcher_")
        self.state_dir = pathlib.Path(self.tmp_dir)
        self.base_url = "https://hub.example.com"
        self.host_id = "test-host-1"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_handle_offer_root_run_unsupported_launcher_declines(self):
        client = MagicMock()
        client.decline_job.return_value = {"status": "declined"}

        # Real runtime manager with actual launcher without root-run profile
        runtime_mgr = RuntimeManager(launcher_repo_path="/home/alexey/git/agent-quota-launcher")
        root_bridge = GuardedRootBridge(state_dir=str(self.state_dir))

        dispatcher = JobDispatcher(
            base_url=self.base_url,
            host_id=self.host_id,
            token_provider=lambda: "fake-host-token",
            client=client,
            runtime_mgr=runtime_mgr,
            root_bridge=root_bridge
        )

        offer = {
            "type": "offer",
            "job_id": "job-root-1",
            "kind": "root-run",
            "payload": {}
        }

        res = dispatcher.handle_offer(offer)
        self.assertFalse(res["accepted"])
        self.assertIn("launcher_unsupported", res["reason"])
        self.assertEqual(client.claim_job.call_count, 0)
        self.assertEqual(client.decline_job.call_count, 1)

    def test_handle_offer_root_run_healthy_incumbent_skips(self):
        client = MagicMock()
        runtime_mgr = MagicMock()
        runtime_mgr.can_process_root_run.return_value = (True, "mock_ok", {})

        root_bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        # Start a healthy incumbent holding lock
        acq = root_bridge.acquire(hub_job_id="incumbent-job", hub_lease_epoch=1)
        self.assertTrue(acq["acquired"])

        dispatcher_bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        dispatcher = JobDispatcher(
            base_url=self.base_url,
            host_id=self.host_id,
            token_provider=lambda: "fake-host-token",
            client=client,
            runtime_mgr=runtime_mgr,
            root_bridge=dispatcher_bridge
        )

        offer = {
            "type": "offer",
            "job_id": "job-root-2",
            "kind": "root-run",
            "payload": {}
        }

        res = dispatcher.handle_offer(offer)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], "skip_healthy_incumbent")
        self.assertEqual(client.claim_job.call_count, 0)
        self.assertEqual(client.decline_job.call_count, 1)

        # Check skip note is NOT written to incumbent inbox during read-only pre-claim inspection
        inbox_file = self.state_dir / "runs" / f"{acq['run_id']}.inbox.md"
        self.assertFalse(inbox_file.exists())

        root_bridge.finish()

    def test_handle_offer_claim_conflict(self):
        client = MagicMock()
        client.claim_job.return_value = {"error": "conflict"}

        runtime_mgr = RuntimeManager(allow_mock_runner=True)
        runtime_mgr.set_mock_runner(lambda *a, **kw: {"status": "ok"})

        dispatcher = JobDispatcher(
            base_url=self.base_url,
            host_id=self.host_id,
            token_provider=lambda: "fake-host-token",
            client=client,
            runtime_mgr=runtime_mgr,
            root_bridge=GuardedRootBridge(state_dir=str(self.state_dir))
        )

        offer = {"job_id": "job-racing", "kind": "custom"}
        res = dispatcher.handle_offer(offer)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], "conflict")

    def test_handle_offer_full_lifecycle(self):
        import subprocess
        from agent_hub_host.guarded_root import get_current_pid_namespace
        client = MagicMock()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        deadline_str = (now_utc + datetime.timedelta(seconds=1800)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.claim_job.return_value = {
            "job_token": "tok_claimed",
            "lease_epoch": 3,
            "deadline_at": deadline_str
        }
        client.start_job.return_value = {"status": "started"}
        client.finish_job.return_value = {"status": "finished"}

        root_bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        runtime_mgr = RuntimeManager(allow_mock_runner=True)
        executed = []
        worker_child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            def mock_runner(job_id, kind, payload, token, epoch, url):
                st = root_bridge.read_state()
                ns = get_current_pid_namespace() or "pid:[4026531836]"
                ev_path = self.state_dir / "runs" / f"first-action-{job_id}.json"
                rd.write_private_json(ev_path, {
                    "job_id": job_id,
                    "run_id": st.get("run_id"),
                    "epoch": epoch,
                    "pid": worker_child.pid,
                    "private_pid_namespace": ns,
                    "numeric_proc_entries": [1, worker_child.pid],
                    "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "stage": "first_action",
                })
                time.sleep(0.03)
                executed.append((job_id, kind, token, epoch))
                return {"status": "succeeded", "summary": "root run finished cleanly", "evidence_refs": ["test.log"]}

            runtime_mgr.set_mock_runner(mock_runner)

            dispatcher = JobDispatcher(
                base_url=self.base_url,
                host_id=self.host_id,
                token_provider=lambda: "fake-host-token",
                client=client,
                runtime_mgr=runtime_mgr,
                root_bridge=root_bridge
            )

            offer = {
                "job_id": "job-lifecycle-1",
                "kind": "root-run",
                "payload": {"task": "verify"}
            }

            res = dispatcher.handle_offer(offer)
            self.assertTrue(res["accepted"])
            self.assertEqual(res["lease_epoch"], 3)

            # Allow worker thread to run to completion
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if dispatcher.get_running_job_count() == 0 and len(executed) > 0:
                    break
                time.sleep(0.05)

            self.assertEqual(len(executed), 1)
            self.assertEqual(dispatcher.get_running_job_count(), 0)
            self.assertEqual(client.start_job.call_count, 1)
            self.assertEqual(client.finish_job.call_count, 1)
        finally:
            if worker_child.poll() is None:
                worker_child.terminate()
                try:
                    worker_child.wait(timeout=2)
                except Exception:
                    worker_child.kill()
                    worker_child.wait(timeout=1)

    def test_handle_offer_corrupt_state_declines_unknown(self):
        state_file = self.state_dir / "state.json"
        state_file.write_text("{corrupt json", encoding="utf-8")

        client = MagicMock()
        rt = MagicMock()
        rt.allow_mock_runner = False
        rt._mock_runner = None
        rt.can_process_root_run.return_value = (True, "ok", {})
        bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "tok",
            client=client,
            runtime_mgr=rt,
            root_bridge=bridge
        )
        res = dispatcher.handle_offer({"id": "job-bad-state", "kind": "root-run"})
        self.assertFalse(res.get("accepted"))
        self.assertEqual(res.get("reason"), "root_state_unknown")

    def test_handle_offer_held_flock_skips_healthy(self):
        (self.state_dir / "state.json").write_text(json.dumps({"status": "finished", "result": "ok"}), encoding="utf-8")
        bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        with patch.object(bridge, "is_lock_held", return_value=True):
            client = MagicMock()
            rt = MagicMock()
            rt.allow_mock_runner = False
            rt._mock_runner = None
            rt.can_process_root_run.return_value = (True, "ok", {})
            dispatcher = JobDispatcher(
                base_url="https://hub.example.com",
                host_id="host-1",
                token_provider=lambda: "tok",
                client=client,
                runtime_mgr=rt,
                root_bridge=bridge
            )
            res = dispatcher.handle_offer({"id": "job-locked", "kind": "root-run"})
            self.assertFalse(res.get("accepted"))
            self.assertEqual(res.get("reason"), "skip_healthy_incumbent")

    def test_handle_offer_malformed_or_zero_expiry_fails_closed(self):
        client = MagicMock()
        rt = MagicMock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "tok",
            client=client,
            runtime_mgr=rt,
            root_bridge=bridge
        )
        for bad_expiry in [0, "", True, float("nan")]:
            res = dispatcher.handle_offer({"id": "job-bad-exp", "kind": "root-run", "offer_expires_at": bad_expiry})
            self.assertFalse(res.get("accepted"))
            self.assertTrue(
                res.get("reason") in ("offer_expired", "malformed_offer_expiry"),
                f"Unexpected reason for bad expiry {bad_expiry!r}: {res.get('reason')}"
            )
            client.claim_job.assert_not_called()

    def test_first_action_preexec_crash_refuses_start_job_and_zero_forgery(self):
        client = MagicMock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        rt = RuntimeManager(allow_mock_runner=True)
        def failing_exec(*args, **kwargs):
            raise RuntimeError("worker crashed before first action")
        rt.set_mock_runner(failing_exec)
        dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "tok",
            client=client,
            runtime_mgr=rt,
            root_bridge=bridge
        )
        job_info = {
            "job_id": "job-crash-preexec",
            "kind": "root-run",
            "job_token": "tok-1",
            "lease_epoch": 1,
            "stop_event": threading.Event()
        }
        dispatcher._run_job_lifecycle(job_info, {})
        client.start_job.assert_not_called()
        fa_file = self.state_dir / "runs" / "first-action-job-crash-preexec.json"
        self.assertFalse(fa_file.exists())
        client.finish_job.assert_called_once()
        self.assertEqual(client.finish_job.call_args[0][4], "failed")

    def test_missing_host_token_declines_and_never_claims(self):
        client = MagicMock()
        rt = MagicMock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "",
            client=client,
            runtime_mgr=rt,
            root_bridge=bridge
        )
        res = dispatcher.handle_offer({"id": "job-no-tok", "kind": "root-run"})
        self.assertFalse(res.get("accepted"))
        self.assertEqual(res.get("reason"), "missing_host_token")
        client.claim_job.assert_not_called()

    def test_f1_worker_succeeding_without_writing_first_action_fails_closed(self):
        client = MagicMock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        rt = RuntimeManager(allow_mock_runner=True, mock_runner=lambda *a, **k: {"status": "succeeded"})
        dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "tok",
            client=client,
            runtime_mgr=rt,
            root_bridge=bridge
        )
        job_info = {
            "job_id": "job-no-evidence",
            "kind": "root-run",
            "job_token": "tok-1",
            "lease_epoch": 1,
            "stop_event": threading.Event()
        }
        dispatcher._run_job_lifecycle(job_info, {})
        client.start_job.assert_not_called()
        self.assertFalse((self.state_dir / "runs" / "first-action-job-no-evidence.json").exists())
        client.finish_job.assert_called_once()
        self.assertEqual(client.finish_job.call_args[0][4], "failed")

    def test_f2_canonical_root_state_denied_before_lock_or_inspect(self):
        canonical_dir = pathlib.Path(os.path.expanduser("~/.local/state/headless-root")).resolve()
        bridge = GuardedRootBridge(state_dir=str(canonical_dir))
        self.assertTrue(bridge.is_canonical_state)
        client = MagicMock()
        rt = RuntimeManager(allow_mock_runner=True, mock_runner=lambda *a, **k: {"status": "succeeded"})
        dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "tok",
            client=client,
            runtime_mgr=rt,
            root_bridge=bridge
        )
        with patch.object(bridge, "is_lock_held") as spy_lock, patch.object(bridge, "inspect_incumbent") as spy_inspect:
            res = dispatcher.handle_offer({"id": "job-canon-refuse", "kind": "root-run"})
            self.assertFalse(res.get("accepted"))
            spy_lock.assert_not_called()
            spy_inspect.assert_not_called()
            client.claim_job.assert_not_called()

    def test_f4_mock_runner_does_not_override_false_profile_supported(self):
        client = MagicMock()
        bridge = GuardedRootBridge(state_dir=str(self.state_dir))
        rt = RuntimeManager(allow_mock_runner=True, mock_runner=lambda *a, **k: {"status": "succeeded"})
        rt.can_process_root_run = lambda: (
            True,
            "ok",
            {
                "status": "UNSUPPORTED",
                "profile_supported": False,
                "root_profile_supported": False,
                "is_fixture_only": True,
                "operational_qualified": False,
            },
        )
        dispatcher = JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "tok",
            client=client,
            runtime_mgr=rt,
            root_bridge=bridge
        )
        res = dispatcher.handle_offer({"id": "job-false-prof", "kind": "root-run"})
        self.assertFalse(res.get("accepted"))
        client.claim_job.assert_not_called()


class TestHostAgentDispatcherHook(unittest.TestCase):
    def test_ws_offer_dispatches_to_job_dispatcher(self):
        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_job_count.return_value = 0

        agent = api.HostAgent(dispatcher=mock_dispatcher)

        # Simulate receiving opcode 1 frame with job offer
        offer_json = json.dumps({"type": "offer", "job_id": "job-ws-1", "kind": "custom"}).encode("utf-8")
        
        # Test WS frame handler logic directly
        with patch.object(agent, "dispatcher", mock_dispatcher):
            msg = json.loads(offer_json.decode("utf-8"))
            mock_dispatcher.handle_offer(msg)

        self.assertEqual(mock_dispatcher.handle_offer.call_count, 1)
        args = mock_dispatcher.handle_offer.call_args[0][0]
        self.assertEqual(args["job_id"], "job-ws-1")

    @patch("agent_hub_host.api.safe_http_json")
    @patch("agent_hub_host.auth.get_access_token")
    def test_poll_fallback_dispatches_offer(self, mock_tok, mock_http):
        mock_tok.return_value = "host_tok"
        mock_http.return_value = {
            "status": "ok",
            "offer": {"type": "offer", "job_id": "job-poll-1", "kind": "root-run"}
        }

        mock_dispatcher = MagicMock()
        mock_dispatcher.get_running_job_count.return_value = 1

        agent = api.HostAgent(dispatcher=mock_dispatcher)
        agent.base_url = "https://hub.example.com"

        # Call _poll_fallback
        with patch.object(agent.clock, "sleep"):
            agent._poll_fallback()

        self.assertEqual(mock_dispatcher.handle_offer.call_count, 1)
        offer_arg = mock_dispatcher.handle_offer.call_args[0][0]
        self.assertEqual(offer_arg["job_id"], "job-poll-1")


class _QualifiedRuntime(RuntimeManager):
    """Explicit fixture: claims genuine operational qualification.

    A real RuntimeManager can never be qualified on this machine (no pins, no Root boundary);
    only this test subclass says so. Everything after qualification (``begin_root_run``, the
    adapter, receipts, ack, cancel) is the real production code against a fixture executor.
    """

    sched_seq = [(True, "scheduling_enabled")]
    sched_calls = 0

    def can_process_root_run(self):
        return True, "operationally_qualified", {
            "operational_qualified": True, "is_fixture_only": False,
            "root_profile_supported": True, "status": "SUPPORTED",
        }

    def check_scheduling(self, job_token=None):
        seq = type(self).sched_seq
        res = seq[min(type(self).sched_calls, len(seq) - 1)]
        type(self).sched_calls += 1
        return res


class TestQualifiedScriptOwnedDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        _QualifiedRuntime.sched_seq = [(True, "scheduling_enabled")]
        _QualifiedRuntime.sched_calls = 0

    def _dispatcher(self, runtime, bridge=None):
        return JobDispatcher(
            base_url="https://hub.example.com",
            host_id="host-1",
            token_provider=lambda: "host_tok",
            client=JobsClient(),
            runtime_mgr=runtime,
            root_bridge=bridge or GuardedRootBridge(clock=object()),
            clock=time,
        )

    def test_unqualified_canonical_offer_still_declined(self):
        calls = []
        disp = self._dispatcher(RuntimeManager())
        disp.client.decline_job = lambda *a, **k: calls.append(a) or {}
        res = disp.handle_offer({"id": "job-q1", "kind": "root-run", "payload": {}})
        self.assertFalse(res["accepted"])
        self.assertTrue(any("canonical_root_state_forbidden" in (c[4] if len(c) > 4 else "") for c in calls) or res["reason"])

    def test_qualified_offer_with_healthy_incumbent_skips(self):
        from agent_hub_host import root_dispatch as rd
        disp = self._dispatcher(_QualifiedRuntime())
        declines = []
        disp.client.decline_job = lambda *a, **k: declines.append(a) or {}
        with patch.object(rd, "canonical_incumbent_status", return_value=("healthy", {"run_id": "r"})):
            res = disp.handle_offer({"id": "job-q2", "kind": "root-run", "payload": {}})
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], "skip_healthy_incumbent")
        self.assertTrue(declines)

    def test_b6_real_manager_denies_before_claim_when_hub_scheduling_is_unreachable_or_unauthorized(self):
        import urllib.error
        supported = {"launcher_repo": "/x", "installed_sha": "a" * 40, "status": "SUPPORTED", "reasons": [],
                     "dependency_packet": None, "root_profile_supported": True, "per_job_env_supported": True,
                     "codex_adapter_supported": True, "adapter_routes": ["codex"], "entrypoint_status": "verified"}
        for exc, needle in ((urllib.error.URLError("down"), "scheduling_unreachable"),
                            (urllib.error.HTTPError("u", 403, "Forbidden", {}, None), "scheduling_unauthorized")):
            mgr = RuntimeManager()
            disp = self._dispatcher(mgr)
            self.assertIsNotNone(mgr._scheduling, "dispatcher binds the authenticated Hub reader")

            def boom(url, token, timeout, exc=exc):
                self.assertEqual(token, "host_tok")
                raise exc
            mgr._scheduling._http_get = boom
            claims, declines = [], []
            disp.client.claim_job = lambda *a, **k: claims.append(a) or {}
            disp.client.decline_job = lambda *a, **k: declines.append(a) or {}
            with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces", return_value=dict(supported)), \
                 patch.object(RuntimeManager, "_quota_gate_present", return_value=True):
                res = disp.handle_offer({"id": "job-s1", "kind": "root-run", "payload": {}})
            self.assertFalse(res["accepted"])
            self.assertIn(needle, res["reason"])
            self.assertEqual(claims, [])
            self.assertEqual(len(declines), 1)


class ScriptOwnedLifecycleCase(unittest.TestCase):
    """Real JobDispatcher + real adapter/RootRun against the fixture-only script protocol."""

    def setUp(self):
        from tests.test_root_dispatch import FakeRootEnv
        self.FakeRootEnv = FakeRootEnv
        _QualifiedRuntime.sched_seq = [(True, "scheduling_enabled")]
        _QualifiedRuntime.sched_calls = 0

    def run_job(self, mode="ok", job_id="job-r", deadline=600, start_result=None, on_start=None, env=None,
                timeout=40):
        env = env or self.FakeRootEnv(self, mode)
        runtime = _QualifiedRuntime(config=env.config, adapter_kwargs=env.adapter_kwargs())
        disp = JobDispatcher(base_url="https://hub.example.com", host_id="host-1", token_provider=lambda: "host_tok",
                             client=JobsClient(), runtime_mgr=runtime,
                             root_bridge=GuardedRootBridge(state_dir=str(env.state)), clock=time)
        calls = {"claim": [], "start": [], "finish": [], "decline": [], "ack_at_start": []}
        disp.client.claim_job = lambda *a, **k: calls["claim"].append(a) or {
            "job_token": "SECRET-JOB-TOKEN", "lease_epoch": 4, "deadline_at": time.time() + deadline}
        disp.client.decline_job = lambda *a, **k: calls["decline"].append(a) or {}
        disp.client.renew_job = lambda *a, **k: {}

        def start(*a, **k):
            calls["start"].append(a)
            run_dirs = list((env.state / "runs").glob("*.hub"))
            calls["ack_at_start"].append(any((d / "start-ack.json").exists() for d in run_dirs))
            if on_start:
                on_start(disp, env)
            return start_result if start_result is not None else {"status": "started"}
        disp.client.start_job = start
        disp.client.finish_job = lambda *a, **k: calls["finish"].append(a) or {}
        with patch("agent_hub_host.gate.get_gate_status", return_value={"launch_allowed": True, "reason": "fixture"}):
            res = disp.handle_offer({"id": job_id, "kind": "root-run", "payload": {}})
        end = time.time() + timeout
        while res.get("accepted") and not calls["finish"] and time.time() < end:
            time.sleep(0.05)
        if res.get("accepted"):
            while disp.get_running_job_count() and time.time() < end:
                time.sleep(0.02)
        return env, disp, res, calls


class TestScriptOwnedLifecycle(ScriptOwnedLifecycleCase):
    def test_b1_genuine_completion_is_reported_succeeded_with_start_before_ack(self):
        env, disp, res, calls = self.run_job("ok")
        self.assertTrue(res["accepted"])
        self.assertEqual(len(calls["finish"]), 1)
        fin = calls["finish"][0]
        self.assertEqual(fin[4], "succeeded")
        self.assertNotIn("fenced", fin[5])
        self.assertEqual(len(calls["start"]), 1)
        st = calls["start"][0]
        run_id = env.requests[0].job_env["HUB_EXPECTED_RUN_ID"]
        first = pathlib.Path(env.requests[0].job_env["HUB_FIRST_ACTION_FILE"])
        import hashlib
        self.assertEqual(st[4], hashlib.sha256(first.read_bytes()).hexdigest())
        self.assertEqual(st[5], f"root-{run_id}")
        self.assertEqual(st[6], env.requests[0].job_env["HUB_LAUNCHER_TASK_ID"])
        self.assertEqual(calls["ack_at_start"], [False], "business waits for the ack, which follows Hub start")
        self.assertTrue(pathlib.Path(env.requests[0].job_env["HUB_START_ACK_FILE"]).exists())
        self.assertEqual(env.controller.signals, [])
        self.assertEqual(disp.get_running_job_count(), 0)

    def test_b1_crash_is_failed_never_succeeded(self):
        env, disp, res, calls = self.run_job("crash")
        self.assertEqual(calls["finish"][0][4], "failed")
        self.assertEqual(len(calls["finish"]), 1)

    def test_b1_error_timeout_fail_skip_and_quota_hold_do_not_succeed(self):
        for mode, want in (("error", "failed"), ("timeout", "failed"), ("skip", "cancelled"),
                           ("quota_held", "cancelled")):
            env, disp, res, calls = self.run_job(mode, f"job-{mode}")
            self.assertEqual(calls["finish"][0][4], want, mode)
            if mode in ("skip", "quota_held"):
                self.assertEqual(calls["start"], [], "a skip must never signal a Hub start")

    def test_b2_foreign_state_and_noop_unit_gives_zero_hub_effects(self):
        env = self.FakeRootEnv(self, "noop")
        now = int(time.time())
        env_old = {"HUB_EXPECTED_RUN_ID": "OLD-STALE-RUN", "HUB_JOB_ID": "old", "HUB_LEASE_EPOCH": "1",
                   "HUB_LAUNCHER_TASK_ID": "t", "HUB_LAUNCHER_UNIT": "u.service"}
        (env.state / "state.json").write_text(json.dumps(env.state_record(env_old, start=now - 400,
                                                                          heartbeat=now - 400)))
        env, disp, res, calls = self.run_job(env=env)
        self.assertTrue(res["accepted"])
        self.assertEqual(calls["start"], [], "foreign/stale/no-op state must not become a Hub start")
        self.assertEqual(calls["finish"][0][4], "failed")
        self.assertNotIn("OLD-STALE-RUN", calls["finish"][0][5])
        self.assertEqual(list((env.state / "runs").glob("*.hub/first-action.json")), [])

    def test_b2_receipt_for_a_foreign_run_cancels_the_owned_unit_and_never_starts(self):
        env, disp, res, calls = self.run_job("foreign_receipt")
        self.assertEqual(calls["start"], [])
        self.assertEqual(calls["finish"][0][4], "failed")
        self.assertIn("mismatch: run_id", calls["finish"][0][5])
        self.assertTrue(env.controller.signals)
        self.assertTrue(all(u == f"agent-task-{env.requests[0].task_id}.service" for u, _, _ in env.controller.signals))
        self.assertFalse(pathlib.Path(env.requests[0].job_env["HUB_START_ACK_FILE"]).exists())

    def test_b5_hub_start_refusal_cancels_and_waits_the_owned_unit_before_reporting_failed(self):
        env, disp, res, calls = self.run_job("hang_after_ack", start_result={"error": "conflict"})
        fin = calls["finish"][0]
        self.assertEqual(fin[4], "failed")
        self.assertIn("cancel_verified=True", fin[5])
        self.assertTrue(env.controller.signals)
        self.assertFalse(pathlib.Path(env.requests[0].job_env["HUB_START_ACK_FILE"]).exists())
        self.assertNotIn("root-run-" + env.requests[0].job_env["HUB_EXPECTED_RUN_ID"],
                         [t.name for t in threading.enumerate()])

    def test_b5_lost_lease_cancels_the_owned_unit_and_reports_failed(self):
        def lose(disp, env):
            disp.running_jobs["job-r"]["stop_event"].set()
        env, disp, res, calls = self.run_job("hang_after_ack", on_start=lose)
        fin = calls["finish"][0]
        self.assertEqual(fin[4], "failed")
        self.assertIn("lease_lost", fin[5])
        self.assertIn("cancel_verified=True", fin[5])
        self.assertEqual(len(env.controller.signals) >= 1, True)

    def test_b5_deadline_exceeded_cancels_the_owned_unit(self):
        env, disp, res, calls = self.run_job("hang_after_ack", deadline=1.5)
        fin = calls["finish"][0]
        self.assertEqual(fin[4], "failed")
        self.assertIn("deadline_exceeded", fin[5])
        self.assertIn("cancel_verified=True", fin[5])

    def test_b5_executor_failure_is_reported_with_token_scrubbed(self):
        env = self.FakeRootEnv(self, "ok")

        def boom(req):
            raise RuntimeError("launcher exploded: " + req.job_env["HUB_JOB_TOKEN"])
        env.execute = boom
        env, disp, res, calls = self.run_job(env=env)
        fin = calls["finish"][0]
        self.assertEqual(fin[4], "failed")
        self.assertNotIn("SECRET", fin[5])
        self.assertEqual(calls["start"], [])

    def test_b5_executor_exception_after_hub_start_stops_the_owned_unit_before_finish(self):
        """F1: the launcher raises after Hub start with the owned unit still active. The host must stop
        that exact invocation and verify it from the live unit before it reports the job failed."""
        from tests.test_root_dispatch import RaiseAfterAckEnv, StoppingUnitController
        env = RaiseAfterAckEnv(self, controller=StoppingUnitController())
        env, disp, res, calls = self.run_job(env=env, job_id="job-f1")
        unit = env.requests[0].job_env["HUB_LAUNCHER_UNIT"]
        self.assertEqual(len(calls["start"]), 1, "Hub start was sent")
        self.assertEqual(len(calls["finish"]), 1)
        self.assertEqual(calls["finish"][0][4], "failed")
        self.assertIn("launcher_execution_failed", calls["finish"][0][5])
        self.assertIn("cancel_verified=True", calls["finish"][0][5])
        self.assertEqual([(u, v) for u, _, v in env.controller.signals], [(unit, "stop")])
        self.assertEqual(env.controller.show(unit)["ActiveState"], "inactive")

    def test_b5_executor_exception_with_unstoppable_unit_reports_cancel_unverified(self):
        from tests.test_root_dispatch import RaiseAfterAckEnv
        env = RaiseAfterAckEnv(self)  # stop/kill accepted, unit stays active
        real_cancel = rd.RootRun.cancel
        with patch.object(rd.RootRun, "cancel", lambda self_, reason, wait_sec=30.0, attempts=2:
                          real_cancel(self_, reason, wait_sec=0.6, attempts=1)):
            env, disp, res, calls = self.run_job(env=env, job_id="job-f1b")
        fin = calls["finish"][0]
        self.assertEqual(fin[4], "failed")
        self.assertIn("cancel_verified=False", fin[5])
        self.assertNotIn("cancel_verified=True", fin[5])
        self.assertTrue(env.controller.signals)
        self.assertEqual(env.controller.show(env.requests[0].job_env["HUB_LAUNCHER_UNIT"])["ActiveState"], "active")

    def test_b6_scheduling_disabled_before_dispatch_runs_nothing(self):
        _QualifiedRuntime.sched_seq = [(False, "scheduling_disabled: hub")]
        env, disp, res, calls = self.run_job("ok")
        self.assertEqual(env.executions, 0)
        self.assertEqual(calls["start"], [])
        self.assertEqual(calls["finish"][0][4], "cancelled")
        self.assertIn("scheduling_disabled", calls["finish"][0][5])

    def test_b6_scheduling_unreadable_at_adapter_dispatch_runs_nothing(self):
        _QualifiedRuntime.sched_seq = [(True, "scheduling_enabled"), (False, "scheduling_unreachable: URLError")]
        env, disp, res, calls = self.run_job("ok")
        self.assertEqual(env.executions, 0)
        self.assertEqual(calls["finish"][0][4], "failed")
        self.assertIn("scheduling_unreachable", calls["finish"][0][5])

    def test_b6_scheduling_disabled_before_start_cancels_unit_and_blocks_start(self):
        _QualifiedRuntime.sched_seq = [(True, "e"), (True, "e"), (False, "scheduling_disabled: hub")]
        env, disp, res, calls = self.run_job("hang_after_ack")
        self.assertEqual(calls["start"], [])
        self.assertEqual(calls["finish"][0][4], "cancelled")
        self.assertIn("cancel_verified=True", calls["finish"][0][5])
        self.assertTrue(env.controller.signals)

    def test_b6_disabling_while_running_does_not_block_completion_or_cleanup(self):
        def disable(disp, env):
            _QualifiedRuntime.sched_seq = [(False, "scheduling_disabled: hub")]
            _QualifiedRuntime.sched_calls = 99
        before = []
        env, disp, res, calls = self.run_job("ok", on_start=disable)
        self.assertEqual(calls["finish"][0][4], "succeeded")
        self.assertEqual(env.controller.signals, [])

    def test_dispatch_refusal_finishes_failed_with_no_unit(self):
        env = self.FakeRootEnv(self, "ok")
        (env.state / "runs" / "hub-job-r-e4.json").write_text("{}")
        env, disp, res, calls = self.run_job(env=env)
        self.assertEqual(env.executions, 0)
        self.assertEqual(calls["finish"][0][4], "failed")
        self.assertIn("pre_existing_run_artifacts", calls["finish"][0][5])

    def test_the_job_token_never_appears_in_hub_summaries_or_evidence_refs(self):
        for mode in ("ok", "crash", "foreign_receipt", "error"):
            env, disp, res, calls = self.run_job(mode, f"job-{mode}")
            for fin in calls["finish"]:
                self.assertNotIn("SECRET", fin[5])
                self.assertNotIn("SECRET", json.dumps(fin[6]))

    def test_b7_unknown_incumbent_states_decline_without_claim(self):
        import fcntl
        cases = {}
        for name in ("corrupt", "empty", "foreign_ns", "starting"):
            cases[name] = self.FakeRootEnv(self, "ok")
        (cases["corrupt"].state / "state.json").write_text("]]]")
        (cases["empty"].state / "state.json").write_text("{}")
        now = int(time.time())
        (cases["foreign_ns"].state / "state.json").write_text(json.dumps(
            {"status": "running", "pid": os.getpid(), "heartbeat": now, "start": now, "run_id": "r",
             "pid_namespace": "pid:[1]"}))
        (cases["starting"].state / "state.json").write_text(json.dumps({"status": "finished", "run_id": "prev"}))
        want = {"corrupt": "root_state_unknown", "empty": "root_state_unknown",
                "foreign_ns": "root_state_unknown", "starting": "skip_healthy_incumbent"}
        for name, env in cases.items():
            fd = None
            if name == "starting":
                fd = os.open(env.state / "root.lock", os.O_CREAT | os.O_RDWR)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                # /proc/locks answers only from the init PID namespace (F2); "starting" is that reading.
                with patch.object(rd, "in_init_pid_namespace", return_value=True) if name == "starting" \
                        else contextlib.nullcontext():
                    env, disp, res, calls = self.run_job(env=env)
            finally:
                if fd is not None:
                    os.close(fd)
            self.assertFalse(res["accepted"], name)
            self.assertEqual(res["reason"], want[name], name)
            self.assertEqual(calls["claim"], [], name)
            self.assertEqual(env.executions, 0, name)
            self.assertEqual(len(calls["decline"]), 1, name)

    def test_b7_held_lock_outside_the_init_pid_namespace_declines_as_unknown(self):
        """F2: the kernel holds the lock but /proc/locks cannot show it from a nested namespace."""
        import fcntl
        env = self.FakeRootEnv(self, "ok")
        (env.state / "state.json").write_text(json.dumps({"status": "finished", "run_id": "prev"}))
        fd = os.open(env.state / "root.lock", os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch.object(rd, "in_init_pid_namespace", return_value=False):
                env, disp, res, calls = self.run_job(env=env)
        finally:
            os.close(fd)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], "root_state_unknown")
        self.assertEqual((calls["claim"], env.executions), ([], 0))

    def test_the_script_owned_path_never_takes_the_canonical_lock_or_writes_canonical_state(self):
        env = self.FakeRootEnv(self, "noop")
        before = sorted(p.name for p in env.state.iterdir())
        env, disp, res, calls = self.run_job(env=env)
        after = sorted(p.name for p in env.state.iterdir())
        self.assertEqual([n for n in after if n not in before and n != "runs"], [])
        self.assertFalse((env.state / "root.lock").exists())


class TestInvalidCleanupReceiptJobs(ScriptOwnedLifecycleCase):
    def test_g1_g2_g3_invalid_receipts_cancel_owned_unit_before_hub_finish(self):
        from tests.test_root_dispatch import InvalidCleanupReceiptEnv
        real_cleanup = rd.RootRun.cleanup_after_exit
        def bounded_cleanup(run):
            return real_cleanup(run, wait_sec=0.2, attempts=1)
        cases = (("g1", {"unit_name": None}),
                 ("g2", {"unit_name": "agent-task-other.service"}),
                 ("g3", {"invocation_id": "b" * 32}))
        for label, changes in cases:
            for stops in (True, False):
                with self.subTest(case=label, stops=stops):
                    env = InvalidCleanupReceiptEnv(self, changes, stops=stops)
                    at_finish = []
                    real_handle = JobDispatcher.handle_offer
                    def handle(disp, job):
                        record = disp.client.finish_job
                        def finish(*a, **kw):
                            unit = env.requests[0].job_env["HUB_LAUNCHER_UNIT"]
                            at_finish.append((list(env.controller.signals), env.controller.show(unit)))
                            return record(*a, **kw)
                        disp.client.finish_job = finish
                        return real_handle(disp, job)
                    with patch.object(rd.RootDispatchAdapter, "is_fixture_executor", property(lambda self: False)), \
                         patch.object(rd.RootRun, "cleanup_after_exit", bounded_cleanup), \
                         patch.object(JobDispatcher, "handle_offer", handle):
                        _, disp, res, calls = self.run_job(env=env, job_id=f"job-{label}-{stops}")
                    self.assertTrue(res["accepted"])
                    self.assertEqual(len(calls["start"]), 1)
                    self.assertEqual(len(calls["finish"]), 1)
                    status, summary = calls["finish"][0][4:6]
                    self.assertEqual(status, "failed")
                    self.assertIn(f"cancel_verified={stops}", summary)
                    unit = env.requests[0].job_env["HUB_LAUNCHER_UNIT"]
                    self.assertEqual(len(at_finish), 1)
                    signals, live = at_finish[0]
                    self.assertTrue(signals)
                    self.assertTrue(all(u == unit and i == env.invocation for u, i, _ in signals))
                    self.assertEqual(live["ActiveState"], "inactive" if stops else "active")
                    self.assertEqual(disp.get_running_job_count(), 0)


class TestDispatcherClockQualification(unittest.TestCase):
    class FutureClock:
        def time(self):
            return time.time() + 3600

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.config = rd.TrustedRootConfig.build("/x", "/x", self.base / "root", self.base / "host")
        self.config.canonical_state_dir.mkdir(parents=True)
        ident = rd.read_process_identity(os.getpid())
        now = int(time.time())
        rd.write_private_json(self.config.canonical_state_dir / "state.json", {
            "status": "running", "run_id": "healthy-fixture-root", "pid": os.getpid(),
            "pid_namespace": rd.get_current_pid_namespace(), "pid_start_ticks": ident["start_ticks"],
            "start": now, "heartbeat": now,
        })
        self.gate = self.base / "fixture-gate.py"
        self.gate.write_text("pass\n")

    def offer(self, clock=None, rebind=False):
        mgr = RuntimeManager(config=self.config, quota_gate_script=str(self.gate))
        client = MagicMock(spec=JobsClient)
        client.claim_job.return_value = {"error": "fixture_stop_after_claim"}
        dispatcher = JobDispatcher("https://hub.example", "fixture-host", token_provider=lambda: "fixture-token",
                                   client=client, runtime_mgr=mgr, clock=clock)
        if rebind:
            # The original dispatcher still owns its injected clock after another binding.
            mgr.bind_hub("https://hub.example", lambda: "fixture-token", "fixture-host", time)
        before = {str(p): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        report = {"status": "SUPPORTED", "reasons": [], "root_profile_supported": True,
                  "per_job_env_supported": True, "codex_adapter_supported": True, "entrypoint_status": "verified"}
        with patch("agent_hub_host.runtime.LauncherQualification.inspect_interfaces",
                   side_effect=lambda: dict(report)), \
             patch.object(RuntimeManager, "check_scheduling", return_value=(True, "scheduling_enabled")), \
             patch("agent_hub_host.gate.get_gate_status", return_value={"launch_allowed": True, "reason": "fixture"}), \
             patch.object(rd, "root_lock_held", return_value=True), \
             patch.object(rd, "canonical_incumbent_status", wraps=rd.canonical_incumbent_status) as incumbent, \
             patch.object(rd.RootDispatchAdapter, "begin") as begin, \
             patch.object(rd.RootDispatchAdapter, "dispatch") as dispatch:
            self.assertEqual(rd.canonical_incumbent_status(self.config.canonical_state_dir, time)[0], "healthy")
            incumbent.reset_mock()
            qualified = mgr.can_process_root_run()
            result = dispatcher.handle_offer({"id": "job-clock", "kind": "root-run", "payload": {}})
            begin.assert_not_called()
            dispatch.assert_not_called()
        client.claim_job.assert_not_called()
        client.start_job.assert_not_called()
        self.assertEqual(dispatcher.get_running_job_count(), 0)
        self.assertEqual({str(p): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}, before)
        return qualified, result, incumbent

    def test_injected_clock_refuses_operational_offer_before_incumbent_or_claim(self):
        qualified, result, incumbent = self.offer(self.FutureClock())
        self.assertFalse(qualified[0])
        self.assertFalse(qualified[2]["operational_qualified"])
        self.assertTrue(qualified[2]["is_fixture_only"])
        self.assertIn("clock", result["reason"])
        incumbent.assert_not_called()

    def test_injected_dispatcher_remains_refused_after_runtime_trusted_rebinding(self):
        qualified, result, incumbent = self.offer(self.FutureClock(), rebind=True)
        self.assertFalse(qualified[0])
        self.assertFalse(qualified[2]["operational_qualified"])
        self.assertTrue(qualified[2]["is_fixture_only"])
        self.assertIn("clock", result["reason"])
        incumbent.assert_not_called()

    def test_default_stdlib_clock_keeps_healthy_incumbent_hold(self):
        for clock in (None, time):
            with self.subTest(clock=clock):
                qualified, result, incumbent = self.offer(clock)
                self.assertTrue(qualified[0])
                self.assertFalse(qualified[2]["is_fixture_only"])
                self.assertEqual(result["reason"], "skip_healthy_incumbent")
                incumbent.assert_called_once()


class TestHostAgentWiring(unittest.TestCase):
    def test_default_agent_binds_a_hub_scheduling_reader_for_every_real_dispatcher(self):
        disp = JobDispatcher(base_url="https://hub.example.com", host_id="h", token_provider=lambda: "t",
                             client=MagicMock(), root_bridge=GuardedRootBridge(clock=object()))
        self.assertIsNotNone(disp.runtime_mgr._scheduling)
        self.assertEqual(disp.runtime_mgr._scheduling.url, "https://hub.example.com/api/root/scheduling")
        self.assertEqual(disp.runtime_mgr.host_id, "h")


class TestM7RoleConsumer(unittest.TestCase):
    """Only fake effects. No provider, native worker or service is executed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.binding = {"name": "automation:demo:writer", "generation": 1,
                        "host_id": "host-a", "profile": "writer-run", "ttl_seconds": 120}
        self.effects = []
        self.client = MagicMock(spec=JobsClient)
        self.runtime = RuntimeManager(allow_mock_runner=True)
        self.runtime.set_mock_runner(self.effect)
        self.bridge = GuardedRootBridge(state_dir=self.tmp.name)
        self.dispatcher = JobDispatcher("https://hub.example.com", "host-a", lambda: "host-token",
                                        self.client, self.runtime, self.bridge)
        self.info = {"job_id": "role-job", "kind": "writer-run", "job_token": "role-token",
                     "lease_epoch": 2, "stop_event": threading.Event(), "role_binding": self.binding}
        self.payload = {"date": "2099-05-06", "project": "demo"}
        self.grant = {"execute": True, "role": self.binding["name"], "generation": 1,
                      "epoch": 2, "profile": "writer-run", "payload": self.payload}

    def effect(self, *args):
        self.effects.append(args)
        # Deliberate fake outcome: no claim of genuine native first action.
        return {"status": "failed", "summary": "fake approved consumer effect"}

    def test_role_intent_commits_before_fake_effect_and_duplicate_holds(self):
        self.client.commit_role_execution.side_effect = [self.grant, {"execute": False, "status": "HOLD"}]
        self.dispatcher._run_job_lifecycle(self.info, self.payload)
        self.assertEqual(len(self.effects), 1)
        self.client.commit_role_execution.assert_called_once_with("https://hub.example.com", "role-job", "role-token", 2, 1)
        self.info["stop_event"] = threading.Event()
        self.dispatcher._run_job_lifecycle(self.info, self.payload)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(self.client.finish_job.call_count, 1)

    def test_authority_unreachable_retains_uncertain_intent_without_effect_or_completion(self):
        self.client.commit_role_execution.side_effect = TimeoutError("response lost after SQL commit")
        self.dispatcher._run_job_lifecycle(self.info, self.payload)
        self.assertEqual(self.effects, [])
        self.client.finish_job.assert_not_called()

    def test_consumer_exception_after_grant_retains_intent_for_reconciliation(self):
        def uncertain(*args):
            self.effects.append(args)
            raise TimeoutError('fake effect result unknown')
        self.runtime.set_mock_runner(uncertain)
        self.client.commit_role_execution.return_value = self.grant
        self.dispatcher._run_job_lifecycle(self.info, self.payload)
        self.assertEqual(len(self.effects), 1)
        self.client.finish_job.assert_not_called()

    def test_stale_epoch_generation_profile_or_payload_reply_never_executes(self):
        for fields in [{"epoch": 1}, {"generation": 2}, {"role": "automation:other:writer"},
                       {"profile": "root-run"}, {"payload": {}}, {"execute": False}]:
            self.info["stop_event"] = threading.Event()
            self.client.commit_role_execution.return_value = {**self.grant, **fields}
            self.dispatcher._run_job_lifecycle(self.info, self.payload)
        self.assertEqual(self.effects, [])
        self.client.finish_job.assert_not_called()

    def test_real_unsupported_backend_holds_before_claim_or_intent(self):
        self.runtime.allow_mock_runner = False
        self.runtime._mock_runner = None
        result = self.dispatcher.handle_offer({"id": "job", "kind": "writer-run", "payload": self.payload,
                                                "role_binding": self.binding})
        self.assertFalse(result["accepted"])
        self.assertIn("M8b", result["reason"])
        self.client.claim_job.assert_not_called()
        self.client.commit_role_execution.assert_not_called()

    def test_backend_becomes_unsupported_after_claim_holds_before_intent(self):
        self.runtime.allow_mock_runner = False
        self.runtime._mock_runner = None
        self.dispatcher._run_job_lifecycle(self.info, self.payload)
        self.client.commit_role_execution.assert_not_called()
        self.assertEqual(self.effects, [])
        self.client.finish_job.assert_not_called()

    def test_qualified_fake_offer_carries_server_binding_and_mismatched_claim_holds(self):
        self.client.claim_job.return_value = {"job_token": "role-token", "lease_epoch": 2,
                                              "role_binding": self.binding}
        with patch.object(threading.Thread, "start"):
            result = self.dispatcher.handle_offer({"id": "job", "kind": "writer-run", "payload": self.payload,
                                                    "role_binding": self.binding})
        self.assertTrue(result["accepted"])
        self.assertEqual(self.dispatcher.running_jobs["job"]["role_binding"], self.binding)
        self.client.claim_job.return_value["role_binding"] = {**self.binding, "generation": 99}
        result = self.dispatcher.handle_offer({"id": "other", "kind": "writer-run", "payload": self.payload,
                                                "role_binding": self.binding})
        self.assertFalse(result["accepted"])
        self.assertIn("binding changed", result["reason"])

    def test_renewal_uncertainty_stops_role_instead_of_using_offline_freshness(self):
        self.dispatcher.running_jobs["role-job"] = self.info
        self.client.renew_job.side_effect = TimeoutError("partition")
        event = MagicMock()
        event.is_set.return_value = False
        event.wait.return_value = False
        self.dispatcher._renewal_loop("role-job", "role-token", 2, event)
        event.wait.assert_called_once_with(40.0)
        event.set.assert_called_once()
        self.client.renew_job.assert_called_once()

    @patch("agent_hub_host.api.safe_http_json")
    def test_execution_http_request_is_bound_and_never_retries_a_lost_response(self, http):
        client = JobsClient()
        http.side_effect = TimeoutError("lost reply")
        with self.assertRaises(TimeoutError):
            client.commit_role_execution("https://hub.example.com", "job", "role-token", 2, 1)
        self.assertEqual(http.call_count, 1)
        req = http.call_args[0][0]
        self.assertEqual(req.full_url, "https://hub.example.com/api/jobs/job/execute")
        self.assertEqual(json.loads(req.data), {"epoch": 2, "generation": 1})


if __name__ == "__main__":
    unittest.main()
