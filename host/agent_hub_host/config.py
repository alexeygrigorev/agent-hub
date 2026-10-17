import json
import copy
import os
import sys
from pathlib import Path
from . import private_fs

def get_config_path() -> Path:
    explicit = os.environ.get('HUB_CONFIG_FILE')
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute(): raise ValueError('HUB_CONFIG_FILE must be absolute')
        return path
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not set on Windows")
        base = Path(appdata)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "agent-hub" / "host.json"

import urllib.parse

def is_valid_machine_id(val) -> bool:
    if not isinstance(val, str) or not val or val != val.strip():
        return False
    if len(val) > 128 or any(c.isspace() or ord(c) < 32 for c in val):
        return False
    return True

def canonical_origin(url: str) -> tuple:
    if not isinstance(url, str) or not url or url != url.strip() or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Invalid URL")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only HTTPS URLs or localhost are permitted.")
    host = parsed.hostname.lower()
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Only HTTPS URLs or localhost are permitted.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Invalid URL components")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid port")
    if port is not None and not (0 < int(port) <= 65535):
        raise ValueError("Invalid port")
    eff_port = int(port) if port is not None else (443 if parsed.scheme == "https" else 80)
    return (parsed.scheme.lower(), host, eff_port)

def same_origin(url_a: str, url_b: str) -> bool:
    try:
        return canonical_origin(url_a) == canonical_origin(url_b)
    except Exception:
        return False

class _TokenExchange:
    """One local login/refresh receipt; never ambient config-update authority."""

    def __init__(self, base_url, path, starting_config):
        if path not in ("/api/device/token", "/api/token/refresh"):
            raise ValueError("Invalid token endpoint")
        canonical_origin(base_url)
        self.url = base_url.rstrip("/") + path
        parsed = urllib.parse.urlparse(self.url)
        if parsed.path != path or parsed.query or parsed.params:
            raise ValueError("Invalid token endpoint")
        if starting_config.get("base_url") and not same_origin(base_url, starting_config["base_url"]):
            raise ValueError("Authentication origin mismatch")
        self.base_url = base_url
        self.path = path
        self.starting_config = copy.deepcopy(starting_config)
        self._closed = False
        self._response = self._data = None

    def begin(self, request):
        self._response = self._data = None
        if self._closed or request.full_url != self.url or request.get_method() != "POST":
            raise ValueError("Token exchange request mismatch")

    def record(self, response):
        if self._closed:
            raise ValueError("Token exchange already consumed")
        if not isinstance(response, dict) or "error" in response:
            raise ValueError("Invalid token response")
        # Validate before publishing tokens, including optional public metadata.
        tokens = {k: response[k] for k in ("access_token", "refresh_token") if k in response}
        cleaned = {k: v for k, v in tokens.items() if not (k == "refresh_token" and v is None)}
        if "access_token" not in tokens or _validate_data(tokens) != cleaned:
            raise ValueError("Invalid token response")
        if "machine_id" in response and response["machine_id"] is not None and not is_valid_machine_id(response["machine_id"]):
            raise ValueError("Invalid machine_id in authenticated response")
        if "token_type" in response and (not isinstance(response["token_type"], str) or response["token_type"].lower() != "bearer"):
            raise ValueError("Invalid token response metadata")
        if "expires_in" in response and (type(response["expires_in"]) is not int or response["expires_in"] < 0):
            raise ValueError("Invalid token response metadata")
        if "scope" in response and (not isinstance(response["scope"], str) or any(ord(c) < 32 or ord(c) == 127 for c in response["scope"])):
            raise ValueError("Invalid token response metadata")
        self._response, self._data = response, copy.deepcopy(response)

    def close(self):
        self._closed = True
        self._response = self._data = None

    def commit(self, response, **metadata):
        recorded, data = self._response, self._data
        was_closed = self._closed
        self.close()  # Every attempt, including failed validation/save, consumes it.
        if was_closed or data is None or response is not recorded or response != data:
            raise ValueError("Token exchange response mismatch")
        if set(metadata) - {"client_kind", "client_name", "requested_scopes"}:
            raise ValueError("Invalid authentication metadata")
        if load_config() != self.starting_config:
            raise ValueError("Config changed during authentication; retry login")
        cfg = copy.deepcopy(self.starting_config)
        cfg.update(metadata)
        cfg["access_token"] = data["access_token"]
        if self.path == "/api/device/token":
            cfg["base_url"] = self.base_url
        refresh = data.get("refresh_token", cfg.get("refresh_token") if self.path == "/api/token/refresh" else None)
        if refresh is None:
            cfg.pop("refresh_token", None)
        else:
            cfg["refresh_token"] = refresh
        if "machine_id" in data:
            if data["machine_id"] is None:
                cfg.pop("machine_id", None)
            else:
                cfg["machine_id"] = data["machine_id"]
        save_config(cfg)

def _validate_data(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    res = {}
    for k, v in data.items():
        if k == "refresh_token" and v is None:
            continue
        if k in ("access_token", "refresh_token", "base_url"):
            if not isinstance(v, str) or not v.strip() or v != v.strip():
                return {}
            if any(c.isspace() or ord(c) < 32 for c in v):
                return {}
            if k == "base_url":
                try:
                    canonical_origin(v)
                except Exception:
                    return {}
        elif k == "machine_id":
            if not is_valid_machine_id(v):
                return {}
        res[k] = v
    return res

def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        return _validate_data(json.loads(private_fs.read_bytes(path, 1024 * 1024)))
    except Exception:
        return {}

def save_config(data: dict):
    if not isinstance(data, dict):
        raise ValueError("Config data must be a dict")
    cleaned = {}
    for k, v in data.items():
        if k == "refresh_token" and v is None:
            continue
        cleaned[k] = v
    if cleaned and _validate_data(cleaned) != cleaned:
        raise ValueError("Invalid config data")

    path = get_config_path()
    try:
        private_fs.atomic_write(path, json.dumps(cleaned).encode('utf-8'), 1024 * 1024)
    except Exception:
        raise RuntimeError('Config save failed') from None

def update_config(**kwargs):
    cfg = load_config()
    updates = dict(kwargs)
    if "refresh_token" in updates and updates["refresh_token"] is None:
        updates.pop("refresh_token", None)
        cfg.pop("refresh_token", None)

    if "machine_id" in updates:
        if not is_valid_machine_id(updates["machine_id"]):
            raise ValueError("Invalid machine_id")
        existing_mid = cfg.get("machine_id")
        if existing_mid and existing_mid != updates["machine_id"]:
            raise ValueError("Conflicting machine_id for paired host config")

    cfg.update(updates)
    save_config(cfg)

def clear_config():
    path = get_config_path()
    if path.exists():
        path.unlink()

