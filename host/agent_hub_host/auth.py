import importlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from . import config
from . import api

HOST_SCOPES = ['host', 'bus:send', 'bus:read', 'health:report']
AGENT_SCOPES = ['bus:send', 'bus:read', 'health:report']


def _shared_device_login():
    """The polling loop lives in DataTalksClub/auth."""
    try:
        return importlib.import_module("dtc_device_login")
    except ImportError:
        pass
    roots = []
    if os.environ.get("DTC_AUTH_HOME"):
        roots.append(Path(os.environ["DTC_AUTH_HOME"]))
    here = Path(__file__).resolve()
    for parent in here.parents:
        roots.append(parent / "auth")
        roots.append(parent.parent / "auth")
        if parent.name == "git" or parent == Path("/"):
            break
    for root in roots:
        if (root / "dtc_device_login" / "__init__.py").is_file():
            sys.path.insert(0, str(root))
            return importlib.import_module("dtc_device_login")
    raise RuntimeError(
        "Shared device login was not found. Clone DataTalksClub/auth next to this repo or set DTC_AUTH_HOME."
    )


def _post_device(url, body, _token_exchange=None):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"},
    )
    try:
        if _token_exchange is not None and url == _token_exchange.url:
            return 200, api.safe_http_json(request, _token_exchange=_token_exchange)
        return 200, api.safe_http_json(request)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception as error:
            raise RuntimeError("Token error: malformed response") from error
        if not isinstance(payload, dict):
            payload = {}
        return exc.code, payload


def device_login(base_url: str, client_kind: str, name: str, hostname: str, requested_scopes: list):
    api.check_url(base_url)
    exchange = config._TokenExchange(base_url, "/api/device/token", config.load_config())
    flow = _shared_device_login()
    try:
        token_res = _run_device_login(flow, exchange, base_url, client_kind, name, hostname, requested_scopes)
        exchange.commit(
            token_res,
            client_kind=client_kind,
            client_name=name,
            requested_scopes=list(requested_scopes),
        )
    finally:
        exchange.close()
    print("Login successful.")


def _run_device_login(flow, exchange, base_url, client_kind, name, hostname, requested_scopes):
    try:
        return flow.device_login(
            base_url,
            lambda url, body: _post_device(url, body, exchange),
            start_path="/api/device/code",
            token_path="/api/device/token",
            start_body={
                "client_kind": client_kind,
                "name": name,
                "hostname": hostname,
                "requested_scopes": requested_scopes,
            },
            transient=(urllib.error.URLError,),
        )
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error during device code request: {exc}") from exc
    except flow.DeviceLoginError as exc:
        if exc.code == "access_denied":
            raise RuntimeError("Access denied by user.") from None
        if exc.code in ("expired_token", "timeout"):
            raise RuntimeError("Device code expired.") from None
        if exc.code == "invalid_response":
            raise RuntimeError("The server did not offer device login.") from None
        raise RuntimeError("Device login failed.") from None

import threading
_refresh_lock = threading.Lock()

def refresh_token():
    with _refresh_lock:
        cfg = config.load_config()
        refresh = cfg.get("refresh_token")
        base_url = cfg.get("base_url")
        if not refresh or not base_url:
            raise RuntimeError("No refresh token or base URL available. Please login.")
            
        exchange = config._TokenExchange(base_url, "/api/token/refresh", cfg)
        token_url = exchange.url
        token_payload = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh
        }).encode("utf-8")
        
        req = urllib.request.Request(token_url, data=token_payload, headers={"Content-Type": "application/json"})
        try:
            token_res = api.safe_http_json(req, _token_exchange=exchange)
            exchange.commit(token_res)
            return token_res["access_token"]
        except urllib.error.HTTPError as e:
            should_repair = False
            if e.code == 401:
                should_repair = True
            elif e.code == 400:
                try:
                    body = e.read(1024 * 1024)
                    err_res = json.loads(body.decode("utf-8"))
                    if err_res.get("error") == "invalid_grant":
                        should_repair = True
                except Exception:
                    pass
            
            if should_repair:
                kind = cfg.get('client_kind', 'host')
                name = cfg.get('client_name', 'agent-hub-host')
                scopes = cfg.get('requested_scopes', AGENT_SCOPES if kind == 'agent' else HOST_SCOPES)
                from . import system
                print(f"Refresh failed (code {e.code} revocation), pairing required again.")
                device_login(base_url, kind, name, system.get_hostname(), scopes)
                return get_access_token()
            else:
                raise RuntimeError(f"Transient HTTP error during refresh: {e.code}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error during refresh: {e}")
        finally:
            exchange.close()

def get_access_token() -> str:
    cfg = config.load_config()
    token = cfg.get("access_token")
    if not token:
        raise RuntimeError("Not logged in.")
    return token
