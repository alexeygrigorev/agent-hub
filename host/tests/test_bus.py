import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
from unittest.mock import patch, MagicMock
from io import StringIO
from agent_hub_host import bus

class TestBus(unittest.TestCase):
    @patch("agent_hub_host.api._get_opener")
    @patch("agent_hub_host.auth.get_access_token")
    def test_bus_send(self, mock_get_token, mock_get_opener):
        mock_get_token.return_value = "acc123"
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json"}
        resp.read.return_value = b'{"id": "test-id"}'
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = resp
        mock_get_opener.return_value = mock_opener
        
        with patch.dict(os.environ, {"HUB_URL": "http://127.0.0.1:8787"}):
            res = bus.send("actor1", "actor2", "note", "body", "key1")
            self.assertEqual(res["id"], "test-id")

            # Verify that 'Authorization' doesn't log on error
            mock_opener.open.side_effect = Exception("conn error")
            try:
                bus.inbox("actor1")
            except Exception as e:
                self.assertNotIn("acc123", str(e))

    @patch('agent_hub_host.bus._do_request')
    def test_bus_send_default_idempotency(self, mock_do_req):
        mock_do_req.return_value = {"id": "auto-1"}
        res = bus.send("actor1", "actor2", "note", "body")
        self.assertEqual(res["id"], "auto-1")
        mock_do_req.assert_called_once()
        payload = mock_do_req.call_args[0][2]
        self.assertTrue(len(payload["idempotency_key"]) > 10)

    @patch('agent_hub_host.bus._do_request')
    def test_bus_release_path_with_slashes(self, mock_do_req):
        mock_do_req.return_value = {"status": "ok"}
        bus.release("myproj:src/bus.ts")
        mock_do_req.assert_called_once()
        called_path = mock_do_req.call_args[0][1]
        self.assertEqual(called_path, "/api/bus/claims/myproj%3Asrc%2Fbus.ts/release")

    @patch('agent_hub_host.bus._do_request')
    def test_bus_list_claims(self, mock_do_req):
        mock_do_req.return_value = [{"id": "c1"}]
        res = bus.list_claims(project="myproj", holder="worker-1", active=True)
        self.assertEqual(res, [{"id": "c1"}])
        mock_do_req.assert_called_once()
        called_path = mock_do_req.call_args[0][1]
        self.assertIn("/api/bus/claims?", called_path)
        self.assertIn("project=myproj", called_path)
        self.assertIn("holder=worker-1", called_path)

    @patch('agent_hub_host.bus._do_request')
    def test_cli_parser_missing_arguments(self, mock_do_request):
        from agent_hub_host import cli
        mock_do_request.return_value = {"id": "123"}
        sys_argv_send = ["cli.py", "send", "--from", "a", "--to", "b", "--kind", "note", "--body", "hi", "--idempotency-key", "k1", "--thread-id", "t1"]
        with patch.object(sys, 'argv', sys_argv_send):
            cli.main()
            mock_do_request.assert_called()

        sys_argv_inbox = ["cli.py", "inbox", "--actor", "a", "--limit", "10", "--cursor", "c1"]
        with patch.object(sys, 'argv', sys_argv_inbox):
            cli.main()
            mock_do_request.assert_called()

    @patch('agent_hub_host.bus._do_request')
    def test_cli_stdin_body(self, mock_do_request):
        from agent_hub_host import cli
        mock_do_request.return_value = {"id": "stdin-1"}
        sys_argv = ["cli.py", "send", "--from", "a", "--to", "b", "--kind", "note", "--body", "-"]
        with patch.object(sys, 'argv', sys_argv), patch('sys.stdin', StringIO("hello from stdin")):
            cli.main()
            payload = mock_do_request.call_args[0][2]
            self.assertEqual(payload["body"], "hello from stdin")

    @patch('agent_hub_host.bus._do_request')
    def test_cli_stdin_oversize(self, mock_do_request):
        from agent_hub_host import cli
        sys_argv = ["cli.py", "send", "--from", "a", "--to", "b", "--kind", "note", "--body", "-"]
        oversized = "x" * 16385
        with patch.object(sys, 'argv', sys_argv), patch('sys.stdin', StringIO(oversized)):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 1)

    @patch('agent_hub_host.bus._do_request')
    def test_bus_reply_thread_id(self, mock_do_req):
        mock_do_req.return_value = {"id": "reply-123"}
        res = bus.reply("orig-1", "alice", "bob", "note", "reply body", "k-rep", thread_id="th-99")
        self.assertEqual(res["id"], "reply-123")
        mock_do_req.assert_called_once_with(
            "POST",
            "/api/bus/send",
            {
                "from_actor": "alice",
                "to_actor": "bob",
                "kind": "note",
                "body": "reply body",
                "idempotency_key": "k-rep",
                "thread_id": "th-99",
                "reply_to": "orig-1"
            }
        )

    @patch('agent_hub_host.bus._do_request')
    def test_cli_reply_parser_with_thread_id(self, mock_do_request):
        from agent_hub_host import cli
        mock_do_request.return_value = {"id": "reply-456"}
        sys_argv_reply = [
            "cli.py", "reply",
            "--reply-to", "msg-orig",
            "--from", "alice",
            "--to", "bob",
            "--kind", "note",
            "--body", "hello back",
            "--idempotency-key", "rep-key-1",
            "--thread-id", "th-123"
        ]
        with patch.object(sys, 'argv', sys_argv_reply):
            cli.main()
            mock_do_request.assert_called_with(
                "POST",
                "/api/bus/send",
                {
                    "from_actor": "alice",
                    "to_actor": "bob",
                    "kind": "note",
                    "body": "hello back",
                    "idempotency_key": "rep-key-1",
                    "thread_id": "th-123",
                    "reply_to": "msg-orig"
                }
            )

    @patch('agent_hub_host.bus._do_request')
    def test_cli_claims_list(self, mock_do_request):
        from agent_hub_host import cli
        mock_do_request.return_value = [{"id": "claim-xyz"}]
        sys_argv = ["cli.py", "claims", "--project", "testproj"]
        with patch.object(sys, 'argv', sys_argv):
            cli.main()
            mock_do_request.assert_called_once()

if __name__ == "__main__":
    unittest.main()
