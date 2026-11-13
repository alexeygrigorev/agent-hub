"""Bounded authenticated HTTPS client; secrets never enter error messages."""
import json
import os
import platform
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# Accepted protocol capacity is deliberately independent of capture emission.
# Stage A keeps old 4KiB producers but can replay a future Stage B receipt.
CAPTURE_OUTPUT_BYTES = 64 * 1024
CAPTURE_ACK_BODY_BYTES = 401 * 1024


def _capture_ack(path, body):
    # This is a transport allowance, not command authority. MachineDO chooses
    # the actual limit from its authenticated, persisted command row.
    if (not re.fullmatch(r'/api/sessions/[A-Za-z0-9-]{1,80}/commands/[A-Za-z0-9-]{1,80}/ack', path)
            or not isinstance(body, dict) or set(body) != {'epoch', 'result'}
            or type(body['epoch']) is not int or body['epoch'] < 1):
        return False
    result = body['result']
    if (not isinstance(result, dict) or not set(result) <= {'output', 'capture_binding', 'capture_source'}
            or not isinstance(result.get('output'), str)
            or len(result['output'].encode('utf8')) > CAPTURE_OUTPUT_BYTES):
        return False
    return len(json.dumps({k:v for k,v in result.items() if k != 'output'}).encode()) <= 1024


def private_file(path):
    path = Path(path)
    if os.name == "nt":
        user = subprocess.check_output(["whoami"], text=True).strip()
        permission = "(OI)(CI)F" if path.is_dir() else "F"
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:{permission}"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def save_config(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_file(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".hub2-")
    try:
        private_file(temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if fd is not None:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


class ApiError(RuntimeError):
    def __init__(self,message,status=None,code=None,retry_at=None):
        super().__init__(message)
        self.status,self.code,self.retry_at=status,code,retry_at


def response_error(status, raw):
    code,retry_at=None,None
    raw=raw if isinstance(raw,(bytes,bytearray,str)) else b""
    try:
        body=json.loads(raw) if len(raw)<=4096 else {}
        value=body.get('error')
        if isinstance(value,str) and 0<len(value)<=80 and all(c in 'abcdefghijklmnopqrstuvwxyz_' for c in value):
            code=value
        deadline=body.get('retry_at')
        if (status==429 and code=='budget_paused' and type(deadline) is int
                and time.time()*1000 < deadline <= time.time()*1000+86_405_000):
            retry_at=deadline
    except (OSError,ValueError,AttributeError):
        pass
    return ApiError(f"Hub HTTP {status}",status=status,code=code,retry_at=retry_at)


def _release_query():
    try:
        from ._release import HOST_VERSION, PROTOCOL_VERSION
    except (ImportError, SyntaxError):
        return ""
    if (not isinstance(HOST_VERSION, str) or not re.fullmatch(r"[0-9a-f]{40}", HOST_VERSION)
            or type(PROTOCOL_VERSION) is not int or not 1 <= PROTOCOL_VERSION <= 9007199254740991):
        return ""
    return urllib.parse.urlencode({"host_version": HOST_VERSION, "protocol_version": PROTOCOL_VERSION})


class Client:
    def __init__(self, base_url, token="", timeout=30):
        parsed = urllib.parse.urlsplit(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("HTTPS required outside loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid server URL")
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def request(self, method, path, body=None, key=None, timeout=None):
        if not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise ValueError("API path required")
        headers = {"Accept": "application/json", "User-Agent": "agent-hub2-host/2.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if key:
            headers["Idempotency-Key"] = key
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            limit = CAPTURE_ACK_BODY_BYTES if method == 'POST' and _capture_ack(path, body) else 65536
            if method == 'POST' and path == '/api/heartbeat':
                limit = 262144  # Bounded workspace inventory, independent of captures.
            if len(payload) > limit:
                raise ValueError("request too large")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, payload, headers, method=method)
        # Never forward a bearer token to a redirect destination.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout if timeout is None else min(self.timeout, timeout)) as response:
                data = response.read(1_048_577)
            if len(data) > 1_048_576:
                raise ApiError("response too large")
            return json.loads(data)
        except urllib.error.HTTPError as error:
            try:raw=error.read(4097)
            except OSError:raw=b""
            raise response_error(error.code,raw) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise ApiError("Hub request unavailable") from None

    def socket(self, machine_id, safety=False):
        from .transport import WebSocket
        parsed = urllib.parse.urlsplit(self.base_url)
        query = "safety=operator_stop" if safety else _release_query()
        endpoint = urllib.parse.urlunsplit(("wss" if parsed.scheme == "https" else "ws",
                                            parsed.netloc, parsed.path + "/api/connect", query, ""))
        return WebSocket(endpoint, self.token, self.timeout)

    def publish_native_inventory(self, machine_id, inventory, stop_event=None):
        if not isinstance(machine_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', machine_id):
            raise ValueError('invalid machine identity')
        # Elapsed budget between chunks plus socket inactivity timeouts; an
        # in-flight urllib read is not a hard wall-clock deadline.
        deadline = time.monotonic() + 20
        for chunk in inventory.upload_chunks():
            if stop_event is not None and stop_event.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ApiError('native inventory publication deadline exceeded')
            self.request('POST', '/api/machines/' + machine_id + '/native-inventory', chunk,
                         timeout=min(5, remaining))

    def heartbeat(self, labels, engines=None, capabilities=None, native_sessions=None):
        body = {"os": platform.system().lower(),
                "arch": platform.machine().lower(), "labels": labels,
                "engines": engines or [], "capabilities": capabilities or []}
        if native_sessions is not None:
            body["native_sessions"] = native_sessions
        return self.request("POST", "/api/heartbeat", body)
