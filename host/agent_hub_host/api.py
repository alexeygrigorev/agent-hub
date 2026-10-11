import base64
import hashlib
import json
import os
import random
import socket
import ssl
import struct
import time
import urllib.request
import urllib.error
import urllib.parse
from . import auth, system, config

class Clock:
    def time(self) -> float: return time.monotonic()
    def sleep(self, seconds: float): time.sleep(seconds)

class RNG:
    def random(self) -> float: return random.random()
    def randbytes(self, n: int) -> bytes: return os.urandom(n)

def register_machine(base_url: str):
    info = system.get_system_info()
    url = f"{base_url}/api/machines/register"
    req = urllib.request.Request(url, data=json.dumps(info).encode("utf-8"), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth.get_access_token()}"
    })
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            auth.refresh_token()
            req.add_header("Authorization", f"Bearer {auth.get_access_token()}")
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode("utf-8"))
        raise RuntimeError(f"Registration failed: {e.code}")

def get_available_machines(base_url: str):
    url = f"{base_url}/api/machines/available"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {auth.get_access_token()}"
    })
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            auth.refresh_token()
            req.add_header("Authorization", f"Bearer {auth.get_access_token()}")
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode("utf-8"))
        raise RuntimeError(f"Failed to get available machines: {e.code}")

def get_heartbeat_payload(clock=None) -> dict:
    import psutil
    import shutil
    from . import gate
    
    return {
        "mem_avail_mb": psutil.virtual_memory().available // (1024 * 1024) if hasattr(psutil, "virtual_memory") else 0,
        "disk_free_gb": shutil.disk_usage("/").free // (1024**3),
        "load": os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0),
        "gate": gate.get_gate_status(clock),
        "running_jobs": 0,
        "sessions_summary": {}
    }

def ws_handshake(host: str, port: int, path: str, token: str, is_secure: bool, rng: RNG):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    if is_secure:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    
    sock.connect((host, port))
    key = base64.b64encode(rng.randbytes(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"\r\n"
    ).encode("ascii")
    
    sock.sendall(req)
    
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Connection closed during handshake")
        resp += chunk
        
    head, _, rest = resp.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    if not lines[0].startswith("HTTP/1.1 101"):
        raise ConnectionError(f"Handshake failed: {lines[0]}")
        
    accept = None
    for line in lines[1:]:
        if line.lower().startswith("sec-websocket-accept:"):
            accept = line.split(":", 1)[1].strip()
            
    expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
    if accept != expected:
        raise ConnectionError("Invalid Sec-WebSocket-Accept")
        
    return sock, rest

def ws_send_frame(sock, opcode: int, payload: bytes, rng: RNG):
    mask = rng.randbytes(4)
    masked_payload = bytearray(len(payload))
    for i in range(len(payload)):
        masked_payload[i] = payload[i] ^ mask[i % 4]
        
    header = bytearray([0x80 | opcode])
    if len(payload) < 126:
        header.append(0x80 | len(payload))
    elif len(payload) < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", len(payload)))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", len(payload)))
        
    header.extend(mask)
    sock.sendall(header + masked_payload)

def ws_recv_frame(sock, rest: bytes):
    def read_exact(n):
        nonlocal rest
        while len(rest) < n:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed")
            rest += chunk
        res = rest[:n]
        rest = rest[n:]
        return res

    header = read_exact(2)
    fin = (header[0] & 0x80) != 0
    opcode = header[0] & 0x0F
    has_mask = (header[1] & 0x80) != 0
    length = header[1] & 0x7F
    
    if length == 126:
        length = struct.unpack("!H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(8))[0]
        
    mask = read_exact(4) if has_mask else None
    payload = read_exact(length)
    
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        
    return fin, opcode, payload, rest

class HostAgent:
    def __init__(self, clock=None, rng=None):
        self.clock = clock or Clock()
        self.rng = rng or RNG()
        self.shutdown_requested = False
        self.cfg = config.load_config()
        self.base_url = self.cfg.get("base_url")

    def run(self):
        register_machine(self.base_url)
        
        parsed = urllib.parse.urlparse(self.base_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{ws_scheme}://{parsed.netloc}/api/host/connect"
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = "/api/host/connect"
        
        backoff = 1.0
        
        while not self.shutdown_requested:
            try:
                token = auth.get_access_token()
                sock, rest = ws_handshake(host, port, path, token, parsed.scheme == "https", self.rng)
                self._run_ws(sock, rest)
                backoff = 1.0
            except Exception as e:
                print(f"WS error: {e}, falling back to polling")
                try:
                    self._poll_fallback()
                    backoff = 1.0
                except Exception as pe:
                    print(f"Poll error: {pe}")
                    self.clock.sleep(backoff)
                    backoff = min(600.0, backoff * 2.0)

    def _run_ws(self, sock, rest):
        sock.settimeout(1.0)
        last_hb = self.clock.time()
        
        while not self.shutdown_requested:
            now = self.clock.time()
            if now - last_hb >= 60.0:
                hb = get_heartbeat_payload(self.clock)
                ws_send_frame(sock, 1, json.dumps(hb).encode("utf-8"), self.rng)
                last_hb = now
                
            try:
                fin, opcode, payload, rest = ws_recv_frame(sock, rest)
                if opcode == 9: # Ping
                    ws_send_frame(sock, 10, payload, self.rng)
                elif opcode == 8: # Close
                    return
                elif opcode == 1: # Text
                    msg = json.loads(payload.decode("utf-8"))
                    print(f"Received msg: {msg}")
            except socket.timeout:
                pass

    def _poll_fallback(self):
        url = f"{self.base_url}/api/host/poll"
        hb = get_heartbeat_payload(self.clock)
        req = urllib.request.Request(url, data=json.dumps(hb).encode("utf-8"), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth.get_access_token()}"
        })
        try:
            with urllib.request.urlopen(req, timeout=10.0) as res:
                json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                auth.refresh_token()
            raise
        
        jitter = (self.rng.random() * 0.4 - 0.2) * 30.0 # 30s +- 20%
        self.clock.sleep(30.0 + jitter)
