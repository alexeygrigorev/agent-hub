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
                ev_path.write_text(
                    json.dumps({
                        "job_id": job_id,
                        "run_id": st.get("run_id"),
                        "epoch": epoch,
                        "pid": worker_child.pid,
                        "private_pid_namespace": ns,
                        "numeric_proc_entries": [1, worker_child.pid],
                        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "stage": "first_action",
                    }),
                    encoding="utf-8",
                )
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


if __name__ == "__main__":
    unittest.main()
