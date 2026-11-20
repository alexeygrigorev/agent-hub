import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agent_hub_host.cli import main
from agent_hub_host.client import save_config


class Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.calls.append((self.command, self.path, json.loads(body) if body else None, self.headers.get("Authorization")))
        response = {"ok": True}
        parsed = urlsplit(self.path)
        if parsed.path == "/api/repos":
            response = {"items": [{"name": "second" if "after" in parse_qs(parsed.query) else "first"}],
                        "next": None if "after" in parse_qs(parsed.query) else "first"}
        if parsed.path == "/api/enrollments/complete":
            response = {"token": "synthetic-machine-token", "machine_id": "test-machine"}
        if parsed.path == "/api/messages/message-1":
            response = {"message": {"id": "message-1", "sender": "agent:other", "epoch": 3}}
        if parsed.path=='/api/intakes':
            q=parse_qs(parsed.query)
            response={'items':[{'id':'second' if 'cursor' in q else 'first'}], 'next':None if 'cursor' in q else '[123,"first"]'}
        if parsed.path=='/api/intakes/task-1/history':
            response={'events':[{'seq':1}],'next':None}
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = handle_request
    do_POST = handle_request
    do_PATCH = handle_request


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "client.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        Handler.calls = []
        save_config(self.config, {"server": f"http://127.0.0.1:{self.server.server_port}", "token": "synthetic-token"})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def invoke(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--config", str(self.config), *arguments])
        return code, json.loads(output.getvalue()) if output.getvalue() else None

    def test_repo_all_follows_keyset_pages_and_preserves_status(self):
        code, result = self.invoke("repos", "list", "--status", "active", "--all", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([row["name"] for row in result["items"]], ["first", "second"])
        self.assertEqual(len(Handler.calls), 2)
        query = parse_qs(urlsplit(Handler.calls[1][1]).query)
        self.assertEqual(query["status"], ["active"])
        self.assertEqual(query["after"], ["first"])

    def test_intake_list_uses_cursor_preserves_filters_and_history_keeps_after(self):
        code,result=self.invoke('intake','list','--project','org/repo','--status','archived','--search','synthetic term','--all')
        self.assertEqual(code,0)
        self.assertEqual([row['id'] for row in result['items']],['first','second'])
        query=parse_qs(urlsplit(Handler.calls[-1][1]).query)
        self.assertEqual(query['cursor'],['[123,"first"]'])
        self.assertNotIn('after',query)
        self.assertEqual(query['status'],['archived'])
        self.assertEqual(query['search'],['synthetic term'])
        self.assertEqual(self.invoke('intake','list','--project','org/repo','--cursor','[123,"first"]')[0],0)
        self.assertIn('cursor',parse_qs(urlsplit(Handler.calls[-1][1]).query))
        self.assertEqual(self.invoke('intake','history','task-1','--project','org/repo','--after','8')[0],0)
        self.assertEqual(parse_qs(urlsplit(Handler.calls[-1][1]).query)['after'],['8'])

    def test_intake_creates_verbatim_text_and_criteria_with_project(self):
        text = Path(self.temp.name) / "ask.txt"
        text.write_text("Keep this exact ask.\nSecond line.")
        code, _ = self.invoke("intake", "create", "--project", "canonical-project", "--title", "Task",
                              "--text-file", str(text), "--acceptance-criteria", "First requirement", "--key", "ask-1")
        self.assertEqual(code, 0)
        method, url, body, auth = Handler.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(parse_qs(urlsplit(url).query)["project"], ["canonical-project"])
        self.assertEqual(body["text"], text.read_text())
        self.assertEqual(body["acceptance_criteria"], ["First requirement"])
        self.assertEqual(auth, "Bearer synthetic-token")

    def test_intake_append_posts_merge_evidence_and_merge_check_collects_for_requested_pr(self):
        from agent_hub_host import merge_evidence

        proof = {"kind": "merge_gate", "repository": "org/repo", "checked_at": "2026-11-13T10:00:00Z"}
        evidence = Path(self.temp.name) / "merge.json"
        evidence.write_text(json.dumps({"evidence": proof}), encoding="utf-8")
        code, _ = self.invoke("intake", "append", "task-1", "--project", "org/repo", "--key", "merge-1", "--file", str(evidence))
        self.assertEqual(code, 0)
        method, path, body, _ = Handler.calls[-1]
        self.assertEqual((method, urlsplit(path).path), ("POST", "/api/intakes/task-1/append"))
        self.assertEqual(body, {"evidence": proof, "key": "merge-1"})

        verdict = {"kind": "acceptance_verification", "session_id": "v1", "verdict": "pass",
                   "summary": "All met", "acceptance": [{"status": "met", "proof": "ok"}]}
        evidence.write_text(json.dumps(verdict), encoding="utf-8")
        code, _ = self.invoke("intake", "append", "task-1", "--project", "org/repo", "--key", "verify-1", "--file", str(evidence))
        self.assertEqual(code, 0)
        method, path, body, _ = Handler.calls[-1]
        self.assertEqual((method, urlsplit(path).path), ("POST", "/api/intakes/task-1/append"))
        self.assertEqual(body, {"evidence": verdict, "key": "verify-1"})

        with patch.object(merge_evidence, "collect", return_value=proof) as collect:
            code, _ = self.invoke("intake", "merge-check", "task-1", "--project", "org/repo", "--pr", "23", "--key", "merge-check-1")
        self.assertEqual(code, 0)
        collect.assert_called_once_with(None, "23")
        method, path, body, _ = Handler.calls[-1]
        self.assertEqual((method, urlsplit(path).path), ("POST", "/api/intakes/task-1/merge-evidence"))
        self.assertEqual(body, {"key": "merge-check-1", "evidence": proof})

    def test_sessions_finish_requires_a_matching_record_and_posts_to_intake_finish_route(self):
        event = Path(self.temp.name) / "finished.json"
        event.write_text(json.dumps({"kind": "session_finished", "session_id": "session-1", "summary": "Done",
                                     "acceptance": [{"status": "met", "proof": "PR 23"}]}), encoding="utf-8")
        code, _ = self.invoke("sessions", "finish", "session-1", "--intake-task-id", "task-1", "--key", "finish-1", "--file", str(event))
        self.assertEqual(code, 0)
        method, path, body, _ = Handler.calls[-1]
        self.assertEqual((method, urlsplit(path).path), ("POST", "/api/intakes/task-1/finished"))
        self.assertEqual(body["kind"], "session_finished")
        self.assertEqual(body["session_id"], "session-1")
        self.assertEqual(body["key"], "finish-1")

        event.write_text(json.dumps({"kind": "session_finished", "session_id": "somebody-else", "acceptance": []}), encoding="utf-8")
        call_count = len(Handler.calls)
        self.assertEqual(self.invoke("sessions", "finish", "session-1", "--intake-task-id", "task-1", "--file", str(event))[0], 1)
        self.assertEqual(len(Handler.calls), call_count, "mismatched session evidence never reaches Hub")

    def test_session_capture_uses_fenced_command_surface(self):
        code, _ = self.invoke("sessions", "capture", "session-1", "--machine", "test-host", "--key", "capture-1")
        self.assertEqual(code, 0)
        method, path, body, _ = Handler.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(urlsplit(path).path, "/api/sessions/session-1/commands")
        self.assertEqual(body, {"kind": "capture", "key": "capture-1"})
        self.assertEqual(parse_qs(urlsplit(path).query)["machine_id"], ["test-host"])

    def test_auto_start_omits_overrides_and_explicit_model_preserves_reason(self):
        common=("sessions","start","--project","org/repo","--intake-task-id","task-1","--key","start-1")
        self.assertEqual(self.invoke(*common)[0],0)
        body=Handler.calls[-1][2]
        self.assertNotIn("machine_id",body)
        self.assertNotIn("model",body)
        self.assertNotIn("engine",body)
        self.assertEqual(self.invoke(*common,"--model","actual-model","--reason","reproduce failure","--with-goal")[0],0)
        body=Handler.calls[-1][2]
        self.assertEqual(body["model"],"actual-model")
        self.assertEqual(body["override_reason"],"reproduce failure")
        self.assertTrue(body["with_goal"])

    def test_reply_discovers_sender_and_show_records_epoch_bound_read(self):
        code, _ = self.invoke("msg", "reply", "message-1", "--body", "Reply", "--key", "reply-1")
        self.assertEqual(code, 0)
        self.assertEqual(Handler.calls[-1][2], {"reply_to": "message-1", "parent_sender": "agent:other",
                                               "recipient": "agent:other", "body": "Reply", "key": "reply-1"})
        code, _ = self.invoke("msg", "show", "message-1")
        self.assertEqual(code, 0)
        self.assertEqual(urlsplit(Handler.calls[-1][1]).path, "/api/messages/message-1/read")
        self.assertEqual(Handler.calls[-1][2], {"epoch": 3})

    def test_missing_project_and_out_of_range_page_make_no_http_request(self):
        self.assertEqual(self.invoke("intake", "list")[0], 1)
        self.assertEqual(self.invoke("repos", "list", "--limit", "101")[0], 1)
        self.assertEqual(Handler.calls, [])

    def test_enrollment_defaults_to_live_server_when_config_has_no_server(self):
        from agent_hub_host import cli

        save_config(self.config, {})
        with patch.object(cli, "DEFAULT_SERVER", f"http://127.0.0.1:{self.server.server_port}"), \
             patch.dict(os.environ, {"HUB2_PAIRING_CODE": "synthetic-pairing-code"}):
            code, result = self.invoke("enroll", "--root", self.temp.name, "--engine", "codex")

        self.assertEqual(code, 0)
        self.assertEqual(result, {"enrolled": "test-machine"})
        method, path, body, _ = Handler.calls[-1]
        self.assertEqual((method, urlsplit(path).path), ("POST", "/api/enrollments/complete"))
        self.assertEqual(body, {"code": "synthetic-pairing-code"})
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["server"], f"http://127.0.0.1:{self.server.server_port}")

    def test_heartbeat_command_only_posts_machine_heartbeat(self):
        save_config(self.config, {
            "server": f"http://127.0.0.1:{self.server.server_port}",
            "token": "synthetic-token",
            "machine_id": "test-machine",
            "roots": [self.temp.name],
            "engines": ["shell"],
            "refresh_token": "synthetic-refresh-token",
            "expires_at": 0,
        })
        saved_config = self.config.read_bytes()
        with patch("agent_hub_host.cli.Journal") as journal, \
             patch("agent_hub_host.agent.Bridge.strict_wake_supported", return_value=True), \
             patch("agent_hub_host.auth.refresh") as refresh, \
             patch("agent_hub_host.native_launch.shutil.which", return_value=None):
            code, result = self.invoke("heartbeat", "--label", "fleet-cycle", "--aplexer", os.sys.executable)

        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(Handler.calls), 1)
        method, path, body, auth = Handler.calls[0]
        self.assertEqual((method, urlsplit(path).path), ("POST", "/api/heartbeat"))
        self.assertEqual(auth, "Bearer synthetic-token")
        self.assertEqual(body["labels"], ["fleet-cycle"])
        self.assertEqual(body["engines"], ["shell"])
        self.assertEqual(body["capabilities"], ["headless", "modes:v1:shell:h", "repo-identity:v1", "workspace-preflight:v1", "session-wakes:strict-inbox:v1"])
        self.assertNotIn("native_sessions", body)
        journal.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(self.config.read_bytes(), saved_config)

    def test_intake_update_parent(self):
        code, _ = self.invoke("intake", "update", "0cfeba5e", "--project", "DataTalksClub/website", "--parent", "bf3a67fb", "--key", "p1")
        self.assertEqual(code, 0)
        method, path, body, _ = Handler.calls[-1]
        self.assertEqual((method, urlsplit(path).path), ("PATCH", "/api/intakes/0cfeba5e"))
        self.assertEqual(parse_qs(urlsplit(path).query)["project"], ["DataTalksClub/website"])
        self.assertEqual(body, {"key": "p1", "parent_intake_id": "bf3a67fb"})

        code, _ = self.invoke("intake", "update", "0cfeba5e", "--project", "DataTalksClub/website", "--parent", "none", "--key", "p2")
        self.assertEqual(code, 0)
        self.assertIsNone(Handler.calls[-1][2]["parent_intake_id"])

    def test_serve_natural_startup_hook_runs_only_for_continuous_serve_and_never_gates(self):
        save_config(self.config, {
            "server": f"http://127.0.0.1:{self.server.server_port}",
            "token": "synthetic-token",
            "machine_id": "test-machine",
            "roots": [self.temp.name],
            "engines": ["shell"],
        })
        with patch("agent_hub_host.quota_natural_exec.maybe_exec_natural_host",
                   side_effect=RuntimeError("no_helper")) as hook, \
             patch("agent_hub_host.cli.Bridge") as bridge:
            code, _ = self.invoke("serve", "--label", "l1")
        self.assertEqual(code, 0)
        hook.assert_called_once_with(["--config", str(self.config), "serve", "--label", "l1"])
        bridge.return_value.serve.assert_called_once()

        with patch("agent_hub_host.quota_natural_exec.maybe_exec_natural_host") as hook, \
             patch("agent_hub_host.cli.Bridge") as bridge:
            code, _ = self.invoke("serve", "--once")
        self.assertEqual(code, 0)
        hook.assert_not_called()
        bridge.return_value.drain.assert_called_once()

        with patch("agent_hub_host.quota_natural_exec.maybe_exec_natural_host") as hook, \
             patch("agent_hub_host.quota_natural_startup.consume_natural_handoff",
                   side_effect=RuntimeError("bad_fd")), \
             patch("agent_hub_host.cli.Bridge") as bridge:
            code, _ = self.invoke("serve", "--alert-handoff-fd", "9")
        self.assertEqual(code, 0)
        hook.assert_not_called()
        bridge.return_value.serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
