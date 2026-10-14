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


def _post_device(url, body):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"},
    )
    try:
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
    flow = _shared_device_login()
    try:
        token_res = flow.device_login(
            base_url,
            _post_device,
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
        raise RuntimeError(str(exc)) from None
    config.update_config(
        access_token=token_res["access_token"],
        refresh_token=token_res.get("refresh_token"),
        base_url=base_url,
    )
    print("Login successful.")

import threading
_refresh_lock = threading.Lock()

def refresh_token():
    with _refresh_lock:
        cfg = config.load_config()
        refresh = cfg.get("refresh_token")
        base_url = cfg.get("base_url")
        if not refresh or not base_url:
            raise RuntimeError("No refresh token or base URL available. Please login.")
            
        token_url = f"{base_url}/api/token/refresh"
        token_payload = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh
        }).encode("utf-8")
        
        req = urllib.request.Request(token_url, data=token_payload, headers={"Content-Type": "application/json"})
        try:
            token_res = api.safe_http_json(req)
            config.update_config(
                access_token=token_res["access_token"],
                refresh_token=token_res.get("refresh_token", refresh)
            )
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
                config.clear_config()
                config.update_config(base_url=base_url)
                from . import system
                print(f"Refresh failed (code {e.code} revocation), pairing required again.")
                device_login(base_url, "host", "agent-hub-host", system.get_hostname(), ["host_agent"])
                return get_access_token()
            else:
                raise RuntimeError(f"Transient HTTP error during refresh: {e.code}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error during refresh: {e}")

def get_access_token() -> str:
    cfg = config.load_config()
    token = cfg.get("access_token")
    if not token:
        raise RuntimeError("Not logged in.")
    return token
