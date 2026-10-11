import urllib.request
import urllib.error
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
from unittest.mock import patch, MagicMock
from agent_hub_host import api

class MockClock:
    def __init__(self):
        self._time = 0.0
    def time(self):
        return self._time
    def sleep(self, s):
        self._time += s
        
class MockRNG:
    def random(self):
        return 0.5
    def randbytes(self, n):
        return b"x" * n

class TestApi(unittest.TestCase):
    @patch("urllib.request.urlopen")
    @patch("agent_hub_host.auth.get_access_token")
    def test_register_machine(self, mock_get_token, mock_urlopen):
        mock_get_token.return_value = "acc123"
        resp = MagicMock()
        resp.read.return_value = b'{"status": "ok"}'
        mock_urlopen.return_value.__enter__.return_value = resp
        
        res = api.register_machine("https://fake")
        self.assertEqual(res, {"status": "ok"})
        
    def test_ws_frame_encoding(self):
        class MockSock:
            def __init__(self):
                self.sent = b""
            def sendall(self, data):
                self.sent += data
        
        sock = MockSock()
        rng = MockRNG()
        from agent_hub_host.ws import WSConnection
        ws = WSConnection(sock, b"", rng)
        ws.send_frame(1, b"hello")
        self.assertTrue(len(sock.sent) > 0)
        
    def test_ws_frame_decoding(self):
        class MockSock:
            def recv(self, n):
                return b""
        
        payload = b"hello"
        header = bytearray([0x81, len(payload)])
        rest = bytes(header) + payload
        
        from agent_hub_host.ws import WSConnection
        ws = WSConnection(MockSock(), rest, MockRNG())
        fin, opcode, decoded = ws.recv_frame()
        self.assertTrue(fin)
        self.assertEqual(opcode, 1)
        self.assertEqual(decoded, b"hello")

    @patch("urllib.request.urlopen")
    @patch("agent_hub_host.auth.get_access_token")
    def test_bearer_not_forwarded(self, mock_get_token, mock_urlopen):
        mock_get_token.return_value = "acc123"
        # We need to test if the custom NoRedirectHandler raises HTTPError rather than forwarding Bearer token
        from agent_hub_host.api import NoRedirectHandler
        handler = NoRedirectHandler()
        req = urllib.request.Request("https://fake")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", None, "https://other")

    @patch("urllib.request.urlopen")
    @patch("agent_hub_host.auth.get_access_token")
    def test_no_token_in_exception(self, mock_get_token, mock_urlopen):
        mock_get_token.return_value = "secret_token_123"
        mock_urlopen.side_effect = Exception("Some connection error")
        
        try:
            api.register_machine("https://fake")
        except Exception as e:
            self.assertNotIn("secret_token_123", str(e))
