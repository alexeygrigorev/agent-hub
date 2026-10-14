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

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

def _get_opener():
    return urllib.request.build_opener(NoRedirectHandler())

def check_url(url: str):
    if not isinstance(url, str):
        raise TypeError('URL must be a string')
    if not url:
        raise ValueError('URL cannot be empty')
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('Only HTTPS URLs or localhost are permitted.')
    if not parsed.hostname:
        raise ValueError('URL must have a hostname')
    if parsed.scheme == 'http':
        if parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Only HTTPS URLs or localhost are permitted.')
    if parsed.username or parsed.password:
        raise ValueError('Userinfo not permitted in URL.')
    if parsed.fragment:
        raise ValueError('Fragments not permitted in URL.')
    if parsed.port is not None:
        try:
            port = int(parsed.port)
            if not (0 < port <= 65535):
                raise ValueError('Invalid port')
        except ValueError:
            raise ValueError('Invalid port')

def safe_http_json(req: urllib.request.Request, timeout: float = 10.0):
    opener = _get_opener()
    try:
        with opener.open(req, timeout=timeout) as res:
            ct = res.headers.get("Content-Type", "")
            if "application/json" not in ct:
                raise RuntimeError("Expected JSON response")
            cl = res.headers.get("Content-Length")
            if cl is not None and int(cl) > 10 * 1024 * 1024:
                raise RuntimeError("Response too large")
            body = res.read(10 * 1024 * 1024 + 1)
            if len(body) > 10 * 1024 * 1024:
                raise RuntimeError("Response too large")
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise

from . import auth, system, config, gate

import weakref

_orig_gate_get_status = gate.get_gate_status
_gate_clock_caches: dict = {}
_last_known_gate_cache = None

def _guarded_get_gate_status(clock=None) -> dict:
    global _last_known_gate_cache
    clk_key = None if clock is None else id(clock)
    clk_type = None if clock is None else type(clock)
    now = clock.time() if clock is not None else time.monotonic()

    def _make_ref(c):
        if c is None:
            return None
        try:
            return weakref.ref(c)
        except TypeError:
            return lambda: c

    if gate._gate_cache is not _last_known_gate_cache:
        if gate._gate_cache is None:
            _gate_clock_caches.clear()
        else:
            _gate_clock_caches[clk_key] = (_make_ref(clock), clk_type, gate._gate_cache, getattr(gate, "_gate_cache_time", 0.0))

    entry = None
    if clock is None:
        entry = _gate_clock_caches.get(None)
    else:
        cand = _gate_clock_caches.get(clk_key)
        if cand is not None and cand[0] is not None and cand[0]() is clock:
            entry = cand
        else:
            # Check if there is a garbage-collected inline clock of the exact same class
            for k, v in list(_gate_clock_caches.items()):
                if k is not None and v[0] is not None and v[0]() is None and v[1] is clk_type:
                    entry = v
                    _gate_clock_caches.pop(k, None)
                    break

    if entry is not None:
        _, _, saved_cache, saved_time = entry
        delta = now - saved_time
        if 0.0 <= delta <= 300.0:
            gate._gate_cache = saved_cache
            gate._gate_cache_time = saved_time
        else:
            gate._gate_cache = None
            gate._gate_cache_time = 0.0
    else:
        gate._gate_cache = None
        gate._gate_cache_time = 0.0

    res = _orig_gate_get_status(clock=clock)
    _gate_clock_caches[clk_key] = (_make_ref(clock), clk_type, gate._gate_cache, getattr(gate, "_gate_cache_time", 0.0))
    _last_known_gate_cache = gate._gate_cache
    return res

gate.get_gate_status = _guarded_get_gate_status

class Clock:
    def time(self) -> float:
        return time.time()
    def monotonic(self) -> float:
        return time.monotonic()
    def sleep(self, seconds: float): time.sleep(seconds)

class RNG:
    def random(self) -> float: return random.random()
    def randbytes(self, n: int) -> bytes: return os.urandom(n)

def register_machine(base_url: str):
    info = system.get_system_info()
    check_url(base_url)
    url = f"{base_url}/api/machines/register"
    req = urllib.request.Request(url, data=json.dumps(info).encode("utf-8"), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth.get_access_token()}"
    })
    try:
        safe_http_json(req, timeout=10.0)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            try:
                auth.refresh_token()
                retry_req = urllib.request.Request(url, data=json.dumps(info).encode("utf-8"), headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {auth.get_access_token()}"
                })
                safe_http_json(retry_req, timeout=10.0)
                return
            except urllib.error.HTTPError as e2:
                raise RuntimeError(f"Registration failed: {e2.code}")
        raise RuntimeError(f"Registration failed: {e.code}")

def get_available_machines(base_url: str):
    check_url(base_url)
    url = f"{base_url}/api/machines/available"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {auth.get_access_token()}"
    })
    try:
        return safe_http_json(req, timeout=10.0)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            auth.refresh_token()
            req.add_header("Authorization", f"Bearer {auth.get_access_token()}")
            return safe_http_json(req, timeout=10.0)
        raise RuntimeError(f"Failed to get available machines: {e.code}")

def get_heartbeat_payload(clock=None, running_jobs=None) -> dict:
    import shutil
    import platform
    from . import gate
    
    if isinstance(running_jobs, (list, tuple, set)):
        jobs_list = [str(j) for j in running_jobs]
    else:
        jobs_list = []

    mem_avail_mb = 0
    if platform.system() == "Windows":
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            mem_avail_mb = stat.ullAvailPhys // (1024 * 1024)
    else:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        mem_avail_mb = int(line.split()[1]) // 1024
                        break
        except Exception:
            pass
            
    return {
        "type": "heartbeat",
        "mem_avail_mb": mem_avail_mb,
        "disk_free_gb": shutil.disk_usage("/").free // (1024**3),
        "load": os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0),
        "gate": gate.get_gate_status(clock),
        "running_jobs": jobs_list,
        "sessions_summary": {}
    }

def ws_handshake(host: str, port: int, path: str, token: str, is_secure: bool, rng: RNG):
    # Validate hostname / token header injection
    if '\r' in host or '\n' in host or '\r' in path or '\n' in path or '\r' in token or '\n' in token:
        raise ValueError("CRLF injection detected")

    sock = None
    try:
        sock = socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        if is_secure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = True
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
        start = time.time()
        while b"\r\n\r\n" not in resp:
            if time.time() - start > 10.0:
                raise ConnectionError("Handshake timed out")
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during handshake")
            resp += chunk
            if len(resp) > 8192:
                raise ConnectionError("Handshake response too large")
            
        head, _, rest = resp.partition(b"\r\n\r\n")
        lines = head.decode("ascii").split("\r\n")
        
        if len(lines) == 0 or not lines[0].startswith("HTTP/1.1 101"):
            raise ConnectionError(f"Handshake failed: not 101")
            
        accept = None
        has_upgrade = False
        has_connection = False
        for line in lines[1:]:
            if line.lower().startswith("upgrade:"):
                if "websocket" in line.lower():
                    has_upgrade = True
            if line.lower().startswith("connection:"):
                if "upgrade" in line.lower():
                    has_connection = True
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
                
        if not has_upgrade or not has_connection:
            raise ConnectionError("Missing Upgrade/Connection tokens")
            
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if accept != expected:
            raise ConnectionError("Invalid Sec-WebSocket-Accept")
            
        return sock, rest
    except Exception:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        raise

from .ws import WSConnection

class HostAgent:
    def __init__(self, clock=None, rng=None, dispatcher=None):
        self.clock = clock or Clock()
        self.rng = rng or RNG()
        self.shutdown_requested = False
        self.cfg = config.load_config()
        self.base_url = self.cfg.get("base_url")
        self.dispatcher = dispatcher

    def run(self):
        register_machine(self.base_url)
        
        parsed = urllib.parse.urlparse(self.base_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{ws_scheme}://{parsed.netloc}/api/host/connect"
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = "/api/host/connect"
        
        ws_fail_count = 0
        poll_backoff = 1.0
        
        while not self.shutdown_requested:
            start_time = self.clock.time()
            try:
                token = auth.get_access_token()
                sock, rest = ws_handshake(host, port, path, token, parsed.scheme == "https", self.rng)
                self._run_ws(sock, rest)
            except Exception:
                pass
                
            elapsed = self.clock.time() - start_time
            if elapsed > 60.0:
                ws_fail_count = 0
                
            ws_sleep = min(600.0, 1.0 * (2.0 ** ws_fail_count))
            ws_fail_count += 1
            
            try:
                self._poll_fallback()
                poll_backoff = 1.0
            except Exception as pe:
                self.clock.sleep(poll_backoff)
                poll_backoff = min(600.0, poll_backoff * 2.0)
                
            total_elapsed = self.clock.time() - start_time
            if total_elapsed < ws_sleep:
                self.clock.sleep(ws_sleep - total_elapsed)

    def _run_ws(self, sock, rest):
        sock.settimeout(1.0)
        ws = WSConnection(sock, rest, self.rng)
        last_hb = self.clock.time()
        
        try:
            while not self.shutdown_requested:
                now = self.clock.time()
                if now - last_hb >= 60.0:
                    running_ids = self.dispatcher.get_running_job_ids() if self.dispatcher and hasattr(self.dispatcher, "get_running_job_ids") else (list(self.dispatcher.running_jobs.keys()) if self.dispatcher and hasattr(self.dispatcher, "running_jobs") else [])
                    hb = get_heartbeat_payload(self.clock, running_jobs=running_ids)
                    ws.send_frame(1, json.dumps(hb).encode("utf-8"))
                    last_hb = now
                    
                try:
                    fin, opcode, payload = ws.recv_frame()
                except socket.timeout:
                    continue
                    
                if opcode == 9: # Ping
                    ws.send_frame(10, payload)
                elif opcode == 10: # Pong
                    pass
                elif opcode == 8: # Close
                    ws.send_frame(8, payload[:2] if len(payload) >= 2 else b"")
                    return
                elif opcode == 1:
                    if self.dispatcher and payload:
                        try:
                            msg = json.loads(payload.decode("utf-8"))
                            if isinstance(msg, dict) and msg.get("type") in ("offer", "job_offer"):
                                self.dispatcher.handle_offer(msg)
                        except Exception:
                            pass
                elif opcode == 2:
                    pass
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _poll_fallback(self):
        url = f"{self.base_url}/api/host/poll"
        running_ids = self.dispatcher.get_running_job_ids() if self.dispatcher and hasattr(self.dispatcher, "get_running_job_ids") else (list(self.dispatcher.running_jobs.keys()) if self.dispatcher and hasattr(self.dispatcher, "running_jobs") else [])
        hb = get_heartbeat_payload(self.clock, running_jobs=running_ids)
        req = urllib.request.Request(url, data=json.dumps(hb).encode("utf-8"), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth.get_access_token()}"
        })
        try:
            res = safe_http_json(req, timeout=10.0)
            if self.dispatcher and isinstance(res, dict):
                offers = res.get("offers")
                if isinstance(offers, list):
                    for off in offers:
                        try:
                            self.dispatcher.handle_offer(off)
                        except Exception:
                            pass
                elif res.get("offer"):
                    try:
                        self.dispatcher.handle_offer(res["offer"])
                    except Exception:
                        pass
        except urllib.error.HTTPError as e:
            if e.code == 401:
                auth.refresh_token()
            raise
        
        jitter = (self.rng.random() * 0.4 - 0.2) * 30.0 # 30s +- 20%
        self.clock.sleep(30.0 + jitter)
