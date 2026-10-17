"""Human Developer CLI profile: device login + status + logout.

- Stores ONLY the human profile (default ~/.config/agent-hub/cli.json,
  separate from legacy host.json; HUB_CONFIG_FILE override wins).
- Uses the ACTUAL shared DTC polling loop (vendored
  _vendored_device_login, same /api/device/code + /api/device/token wire
  shape as DataOps/dapier/shared auth) — never a different protocol.
- Server contract (server worker owns implementation; this client only
  expects it):
    GET  /api/auth/me      safe human identity/status (no tokens)
    POST /api/auth/logout  {} revokes ONLY the human credential family
- Never logs tokens. Prints only the verification URL + user code.
- Reuses the existing _TokenExchange 401-refresh/token-rotation receipt
  discipline with reboot-persistent 0600 atomic config / 0700 dir.
"""
import json
import os
import urllib.request
import urllib.error

from . import api
from . import config
from .auth import HUMAN_SCOPES


def resolve_base_url(explicit=None) -> str:
    if explicit:
        api.check_url(explicit)
        return explicit.rstrip("/") if explicit != explicit.rstrip("/") else explicit
    for env in ("HUB_URL", "HUB_BASE_URL"):
        val = os.environ.get(env)
        if val:
            api.check_url(val)
            return val
    human = config.load_human_config()
    if isinstance(human.get("base_url"), str) and human["base_url"]:
        return human["base_url"]
    # Fresh Linux default (baked, configurable via --url or env).
    return config.DEFAULT_BASE_URL


def _poster(exchange=None):
    def post(url, body):
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            if exchange is not None and url == exchange.url:
                return 200, api.safe_http_json(request, _token_exchange=exchange)
            return 200, api.safe_http_json(request)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception as error:
                raise RuntimeError("Token error: malformed response") from error
            if not isinstance(payload, dict):
                payload = {}
            return exc.code, payload
    return post


def _flow():
    from . import _vendored_device_login as flow
    return flow


def human_login(base_url=None, label=None):
    """Run human device login (kind human) and persist the human profile."""
    from . import system
    base = resolve_base_url(base_url)
    api.check_url(base)
    name = (label or "").strip() or "developer-cli"
    if len(name) > 128:
        raise ValueError("Label too long (max 128)")
    hostname = system.get_hostname()
    scopes = list(HUMAN_SCOPES)
    starting = config.load_human_config()
    exchange = config._TokenExchange(
        base, "/api/device/token", starting,
        _load_fn=config.load_human_config, _save_fn=config.save_human_config,
    )
    flow = _flow()
    try:
        try:
            token_res = flow.device_login(
                base,
                _poster(exchange),
                start_path="/api/device/code",
                token_path="/api/device/token",
                start_body={
                    "client_kind": "human",
                    "label": name,
                    "name": name,
                    "hostname": hostname,
                    "requested_scopes": scopes,
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
        exchange.commit(
            token_res,
            client_kind="human",
            client_name=name,
            requested_scopes=list(scopes),
        )
    finally:
        exchange.close()
    # No tokens to stdout; the loop already printed URL + user code.
    print("Login successful.")


def get_human_access_token() -> str:
    cfg = config.load_human_config()
    if not config.is_human_config(cfg):
        raise RuntimeError("Not logged in as human. Run `hub auth login`.")
    token = cfg.get("access_token")
    if not token:
        raise RuntimeError("Not logged in as human. Run `hub auth login`.")
    return token


def human_refresh() -> str:
    cfg = config.load_human_config()
    if not config.is_human_config(cfg) and not cfg.get("refresh_token"):
        raise RuntimeError("Not logged in as human. Run `hub auth login`.")
    refresh = cfg.get("refresh_token")
    base_url = cfg.get("base_url")
    if not refresh or not base_url:
        raise RuntimeError("Human session expired. Run `hub auth login` again.")
    exchange = config._TokenExchange(
        base_url, "/api/token/refresh", cfg,
        _load_fn=config.load_human_config, _save_fn=config.save_human_config,
    )
    try:
        req = urllib.request.Request(
            exchange.url,
            data=json.dumps({"grant_type": "refresh_token", "refresh_token": refresh}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        token_res = api.safe_http_json(req, _token_exchange=exchange)
        exchange.commit(token_res)
        return token_res["access_token"]
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise RuntimeError("Human session revoked or expired. Run `hub auth login` again.") from None
        raise RuntimeError(f"Transient HTTP error during refresh: {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error during refresh: {exc}") from None
    finally:
        exchange.close()


def _authed_get(path):
    """GET path with human bearer + one 401-refresh retry. Returns parsed JSON."""
    cfg = config.load_human_config()
    base = cfg.get("base_url") or resolve_base_url()
    api.check_url(base)
    token = get_human_access_token()
    url = base.rstrip("/") + path

    def send(access):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
        return api.safe_http_json(req, timeout=10.0)

    try:
        return send(token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            new_token = human_refresh()
            return send(new_token)
        raise


def human_status():
    """Safe human identity/status. Never mistakes a host token for human."""
    cfg = config.load_human_config()
    if not config.is_human_config(cfg):
        return {"logged_in": False}
    try:
        me = _authed_get("/api/auth/me")
    except RuntimeError as exc:
        msg = str(exc)
        if "revoked" in msg or "expired" in msg or "Not logged in" in msg:
            return {"logged_in": False}
        raise
    if not isinstance(me, dict):
        return {"logged_in": True, "base_url": cfg.get("base_url")}
    # Strip anything token-like; keep only safe display fields.
    safe = {"logged_in": True, "base_url": cfg.get("base_url")}
    for key in ("sub", "email", "actor", "kind", "isAdmin", "scopes", "label", "client_name"):
        if key in me and isinstance(me[key], (str, bool, list)):
            safe[key] = me[key]
    label = cfg.get("client_name")
    if label and "label" not in safe:
        safe["label"] = label
    return safe


def human_logout():
    """Revoke ONLY the human family, then remove ONLY the human config.

    Handles already-logged-out (no human profile, or 401 on revoke) and
    always preserves the legacy host file — except single-file override
    mode holding a human profile, which is removed as the owned config.
    """
    cfg = config.load_human_config()
    if not config.is_human_config(cfg):
        # Nothing human to revoke; host file untouched by clear_human_config.
        config.clear_human_config()
        return {"logged_out": True, "already": True}
    base = cfg.get("base_url") or resolve_base_url()
    api.check_url(base)
    url = base.rstrip("/") + "/api/auth/logout"
    token = cfg.get("access_token")
    try:
        req = urllib.request.Request(
            url, data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            api.safe_http_json(req, timeout=10.0)
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 401, 404):
                raise RuntimeError(f"Logout request failed with status {exc.code}") from None
            # 401/404: already revoked/expired on server — still clear local.
    finally:
        config.clear_human_config()
    return {"logged_out": True}
