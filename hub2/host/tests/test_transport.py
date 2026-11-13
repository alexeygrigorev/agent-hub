import base64
import hashlib
import queue
import socket
import struct
import threading
import unittest
from hub2_agent_hub_host.transport import WebSocket


class TransportTests(unittest.TestCase):
    def peer(self,initial=b'',bad_accept=False,redirect=False):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        listener.settimeout(3);received=queue.Queue()
        def server():
            connection,_=listener.accept();connection.settimeout(3)
            raw=b''
            while b'\r\n\r\n' not in raw:
                part=connection.recv(8192)
                if not part:break
                raw+=part
            self.headers=raw.decode()
            key=next(line.split(': ',1)[1] for line in self.headers.split('\r\n') if line.lower().startswith('sec-websocket-key:'))
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if bad_accept:accept='invalid'
            header=f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n'.encode()
            if redirect:header=b'HTTP/1.1 302 Found\r\nLocation: ws://foreign.invalid/\r\nContent-Length: 0\r\n\r\n'
            connection.sendall(header+initial);received.put(connection)
        thread=threading.Thread(target=server,daemon=True);thread.start()
        try:
            ws=WebSocket(f'ws://127.0.0.1:{listener.getsockname()[1]}/api/connect','synthetic-bearer',timeout=2)
        except Exception:
            received.get(timeout=3).close();thread.join(3);listener.close();raise
        peer=received.get(timeout=3);thread.join(3);listener.close()
        self.addCleanup(ws.close);self.addCleanup(peer.close)
        return ws,peer

    def test_real_authenticated_upgrade_preserves_first_coalesced_message(self):
        ws,_=self.peer(b'\x81\x05hello')
        self.assertEqual(ws.recv(timeout=1),'hello')
        self.assertIn('Authorization: Bearer synthetic-bearer',self.headers)
        self.assertNotIn('Sec-WebSocket-Extensions',self.headers)

    def test_fragmented_text_with_ping_is_reassembled_and_pong_masked(self):
        ws,peer=self.peer()
        peer.sendall(b'\x01\x03hel\x89\x01p\x80\x02lo')
        self.assertEqual(ws.recv(timeout=1),'hello')
        pong=peer.recv(100)
        self.assertEqual(pong[:2],b'\x8a\x81')
        self.assertEqual(pong[6]^pong[2],ord('p'))

    def test_timeout_mid_frame_preserves_bytes_for_next_receive(self):
        ws,peer=self.peer();peer.sendall(b'\x81\x05he')
        with self.assertRaises(TimeoutError):ws.recv(timeout=.01)
        peer.sendall(b'llo')
        self.assertEqual(ws.recv(timeout=1),'hello')

    def test_protocol_invalid_and_oversized_frames_fail_closed(self):
        for frame in (b'\x81\x81',b'\xc1\x00',b'\x81\x7f'+struct.pack('!Q',65537),b'\x80\x00',b'\x81\x01\xff'):
            with self.subTest(frame=frame):
                ws,peer=self.peer();peer.sendall(frame)
                with self.assertRaises(RuntimeError):ws.recv(timeout=3)
                peer.close();ws.close()

    def test_binary_application_messages_are_refused(self):
        ws,peer=self.peer();peer.sendall(b'\x82\x01x')
        with self.assertRaisesRegex(RuntimeError,'text_required'):ws.recv(timeout=1)

    def test_client_frames_masked_at_64k_and_larger_send_refused_before_effect(self):
        ws,peer=self.peer();data='x'*65536;received=[]
        def read():
            value=bytearray()
            while len(value)<len(data)+14:value.extend(peer.recv(65550))
            received.append(value)
        thread=threading.Thread(target=read);thread.start();ws.send(data);thread.join(3)
        self.assertFalse(thread.is_alive())
        frame=received[0];self.assertEqual(frame[:2],b'\x81\xff')
        self.assertEqual(int.from_bytes(frame[2:10],'big'),65536)
        mask=frame[10:14]
        self.assertEqual(bytes(v^mask[i%4] for i,v in enumerate(frame[14:])),data.encode())
        with self.assertRaises(ValueError):ws.send(data+'x')

    def test_bad_upgrade_and_redirect_never_become_authenticated_connection(self):
        for options in ({'bad_accept':True},{'redirect':True}):
            with self.subTest(options=options),self.assertRaises(RuntimeError):self.peer(**options)

    def test_insecure_remote_uri_and_header_injection_refused_before_network(self):
        for endpoint,token in [('ws://remote.invalid/','token'),('wss://user:password@remote.invalid/','token'),('wss://remote.invalid/','token\r\nInjected: value')]:
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):WebSocket(endpoint,token)
