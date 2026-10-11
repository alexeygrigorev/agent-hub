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
        api.ws_send_frame(sock, 1, b"hello", rng)
        self.assertTrue(len(sock.sent) > 0)
        
    def test_ws_frame_decoding(self):
        class MockSock:
            def recv(self, n):
                return b""
        
        # Test basic text frame decoding
        payload = b"hello"
        header = bytearray([0x81, len(payload)])
        rest = bytes(header) + payload
        
        fin, opcode, decoded, new_rest = api.ws_recv_frame(MockSock(), rest)
        self.assertTrue(fin)
        self.assertEqual(opcode, 1)
        self.assertEqual(decoded, b"hello")
        self.assertEqual(new_rest, b"")
