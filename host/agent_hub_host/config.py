import json
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

_pending_machine_binding: dict = {}

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

def record_authenticated_machine_binding(url: str, machine_id, access_token: str = None):
    if machine_id is None:
        return
    if not is_valid_machine_id(machine_id):
        raise ValueError("Invalid machine_id in authenticated response")
    origin = canonical_origin(url)
    _pending_machine_binding.clear()
    _pending_machine_binding["origin"] = origin
    _pending_machine_binding["machine_id"] = machine_id
    _pending_machine_binding["access_token"] = access_token

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
    elif _pending_machine_binding.get("machine_id"):
        target_url = updates.get("base_url") or cfg.get("base_url")
        target_tok = updates.get("access_token") or cfg.get("access_token")
        pend_origin = _pending_machine_binding.get("origin")
        pend_tok = _pending_machine_binding.get("access_token")
        pend_mid = _pending_machine_binding.get("machine_id")
        _pending_machine_binding.clear()
        if target_url and pend_origin is not None:
            try:
                if canonical_origin(target_url) == pend_origin and (pend_tok is None or target_tok == pend_tok):
                    existing_mid = cfg.get("machine_id")
                    if existing_mid and existing_mid != pend_mid:
                        raise ValueError("Conflicting machine_id for paired host config")
                    updates["machine_id"] = pend_mid
            except ValueError:
                raise
            except Exception:
                pass

    cfg.update(updates)
    save_config(cfg)

def clear_config():
    _pending_machine_binding.clear()
    path = get_config_path()
    if path.exists():
        path.unlink()

