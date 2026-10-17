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
# Human CLI scopes. The server binds ['admin'] to human device grants at
# approval time (approving allowlisted admin's own identity); the client
# records the same intent locally. Admin is never grantable via the
# host/agent device path (server rejects it there).
HUMAN_SCOPES = ['admin']


def _device_flow():
    """Vendored DTC loop first (no checkout dependency); shared fallback for dev."""
    try:
        from . import _vendored_device_login as flow
        return flow
    except ImportError:
        pass
    return _shared_device_login()


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
    """Legacy host/agent device login. Writes ONLY the host profile.

    Untouched behavior for `hub login URL --kind host|agent`: host.json
    (or HUB_CONFIG_FILE override). Never reads or writes the human
    cli.json. Human login lives in human_auth (kind "human", cli.json).
    """
    api.check_url(base_url)
    probe = config.load_config()
    if not os.environ.get('HUB_CONFIG_FILE') and config.is_human_config(probe):
        # Active profile is human; host pairing must still start from and
        # write to the host file, never the human one.
        starting = config.load_host_config()
        exchange = config._TokenExchange(
            base_url, "/api/device/token", starting,
            _load_fn=config.load_host_config, _save_fn=config.save_host_config,
        )
    else:
        starting = config.load_config()
        exchange = config._TokenExchange(base_url, "/api/device/token", starting)
    flow = _device_flow()
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


def _active_profile():
    """Return (kind, cfg, load_fn, save_fn) for refresh: human first, host fallback.

    Uses load_config() (active, mock-friendly) as the source of truth, then
    routes saves to the file the profile actually came from. Single-file
    override (HUB_CONFIG_FILE): the one file is the profile.
    """
    cfg = config.load_config()
    if os.environ.get('HUB_CONFIG_FILE'):
        return (cfg.get('client_kind') or 'host', cfg, config.load_config, config.save_config)
    if config.is_human_config(cfg):
        return ('human', cfg, config.load_human_config, config.save_human_config)
    # cfg is host (or empty): keep legacy load/save names so existing
    # host tests and `hub login` behavior stay untouched.
    kind = cfg.get('client_kind') or 'host'
    return (kind, cfg, config.load_config, config.save_config)


def refresh_token():
    with _refresh_lock:
        kind, cfg, load_fn, save_fn = _active_profile()
        refresh = cfg.get("refresh_token")
        base_url = cfg.get("base_url")
        if not refresh or not base_url:
            if kind == 'human':
                raise RuntimeError("Human session expired. Run `hub auth login` again.")
            raise RuntimeError("No refresh token or base URL available. Please login.")

        exchange = config._TokenExchange(base_url, "/api/token/refresh", cfg,
                                          _load_fn=load_fn, _save_fn=save_fn)
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
                if kind == 'human':
                    raise RuntimeError("Human session revoked or expired. Run `hub auth login` again.")
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
    """Active access token: human cli.json first, host.json fallback.

    On fresh Linux after `hub auth login` only the human file exists, so
    normal user commands (sessions/intake/status/bus) automatically use it.
    HUB_CONFIG_FILE override returns that single file. Host-only callers
    (register/poll/agent run) must use get_host_access_token().
    """
    cfg = config.load_config()
    token = cfg.get("access_token")
    if not token:
        raise RuntimeError("Not logged in.")
    return token


def get_host_access_token() -> str:
    """Legacy host token only (host.json, or HUB_CONFIG_FILE override)."""
    if os.environ.get('HUB_CONFIG_FILE'):
        cfg = config.load_config()
    else:
        cfg = config.load_host_config()
    token = cfg.get("access_token")
    if not token:
        raise RuntimeError("Not logged in.")
    return token
