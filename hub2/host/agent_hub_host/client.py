"""Bounded authenticated HTTPS client; secrets never enter error messages."""
import json
import os
import platform
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


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


class Client:
    def __init__(self, base_url, token="", timeout=30):
        parsed = urllib.parse.urlsplit(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("HTTPS required outside loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid server URL")
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def request(self, method, path, body=None, key=None):
        if not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise ValueError("API path required")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if key:
            headers["Idempotency-Key"] = key
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            if len(payload) > 65536:
                raise ValueError("request too large")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, payload, headers, method=method)
        # Never forward a bearer token to a redirect destination.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
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
        endpoint = urllib.parse.urlunsplit(("wss" if parsed.scheme == "https" else "ws",
                                            parsed.netloc, parsed.path + "/api/connect", "safety=operator_stop" if safety else "", ""))
        return WebSocket(endpoint, self.token, self.timeout)

    def heartbeat(self, labels, engines=None, capabilities=None, resources=None):
        body = {"os": platform.system().lower(),
                "arch": platform.machine().lower(), "labels": labels,
                "engines": engines or [], "capabilities": capabilities or []}
        if resources is not None:
            body["resources"] = resources
        return self.request("POST", "/api/heartbeat", body)
