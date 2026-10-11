import unittest
import struct
import socket
from agent_hub_host.ws import WSConnection

class MockSocket:
    def __init__(self, data=b""):
        self.data = data
        self.sent = bytearray()
        self.closed = False
        self.timeout_called = False

    def recv(self, size):
        if self.timeout_called:
            raise socket.timeout()
        if not self.data:
            return b""
        chunk = self.data[:size]
        self.data = self.data[size:]
        return chunk

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True

class MockRNG:
    def randbytes(self, n):
        return b"\x00" * n

class TestWSStrict(unittest.TestCase):
    def test_reject_rsv(self):
        # RSV1 set
        sock = MockSocket(b"\xC1\x00")
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("RSV", str(cm.exception))

    def test_reject_reserved_opcode(self):
        sock = MockSocket(b"\x83\x00") # opcode 3
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Reserved opcode", str(cm.exception))

    def test_reject_masked_server(self):
        sock = MockSocket(b"\x81\x80\x00\x00\x00\x00") # masked text frame
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Masked frame from server", str(cm.exception))

    def test_reject_fragmented_control(self):
        sock = MockSocket(b"\x09\x00") # Ping fin=False
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Fragmented control frame", str(cm.exception))

    def test_reject_oversized_control(self):
        sock = MockSocket(b"\x89\x7E\x00\x7E") # Ping size 126
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Oversized control frame", str(cm.exception))

    def test_reject_invalid_length_encoding(self):
        sock = MockSocket(b"\x81\x7E\x00\x7D") # Text, 126 encoding but length 125
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Invalid length encoding", str(cm.exception))

        sock2 = MockSocket(b"\x81\x7F\x00\x00\x00\x00\x00\x00\xFF\xFF") # 127 encoding length 65535
        ws2 = WSConnection(sock2, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws2.recv_frame()
        self.assertIn("Invalid length encoding", str(cm.exception))

    def test_reject_highbit_64bit(self):
        sock = MockSocket(b"\x81\x7F\x80\x00\x00\x00\x00\x00\x00\x00")
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("High bit", str(cm.exception))

    def test_reject_bad_continuation(self):
        sock = MockSocket(b"\x80\x00") # Continuation, fin=True, no prev frame
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Continuation without start", str(cm.exception))

    def test_reject_newdata_while_fragment(self):
        sock = MockSocket(b"\x01\x00\x81\x00") # Text fin=False, then Text fin=True
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("New data frame while fragment open", str(cm.exception))

    def test_reject_invalid_utf8(self):
        sock = MockSocket(b"\x81\x01\xFF") # Text, invalid UTF-8
        ws = WSConnection(sock, b"", MockRNG())
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Invalid UTF-8", str(cm.exception))

    def test_reject_message_too_big(self):
        sock = MockSocket(b"\x01\x7E\xFF\xFF" + (b"A" * 65535))
        ws = WSConnection(sock, b"", MockRNG())
        ws.msg_payload = bytearray(b"A" * (10 * 1024 * 1024))
        with self.assertRaises(ConnectionError) as cm:
            ws.recv_frame()
        self.assertIn("Message too big", str(cm.exception))

    def test_socket_timeout_preserves_state(self):
        sock = MockSocket(b"\x81\x02")
        ws = WSConnection(sock, b"", MockRNG())
        
        # Next recv will throw timeout
        sock.timeout_called = True
        try:
            ws.recv_frame()
        except socket.timeout:
            pass
            
        sock.timeout_called = False
        sock.data = b"\x81\x02HI"
        
        fin, opcode, payload = ws.recv_frame()
        self.assertTrue(fin)
        self.assertEqual(opcode, 1)
        self.assertEqual(payload, b"HI")
        
if __name__ == '__main__':
    unittest.main()
