import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
