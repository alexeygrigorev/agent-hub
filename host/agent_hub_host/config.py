import errno
import json
import copy
import os
import sys
from pathlib import Path
from . import _test_isolation, private_fs

def get_config_path() -> Path:
    explicit = os.environ.get('HUB_CONFIG_FILE')
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute(): raise ValueError('HUB_CONFIG_FILE must be absolute')
        return _forbid_real_user_profile(path)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not set on Windows")
        base = Path(appdata)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return _forbid_real_user_profile(base / "agent-hub" / "host.json")


# Baked default hub base URL for fresh human installs (overridable via
# --url, stored base_url, HUB_URL/HUB_BASE_URL, or HUB_CONFIG_FILE).
DEFAULT_BASE_URL = "https://hub.dtcdev.click"


def get_human_config_path() -> Path:
    """Human CLI profile: ~/.config/agent-hub/cli.json, separate from host.json.

    Explicit HUB_CONFIG_FILE always wins and points both profiles at one
    file (single-file override mode; callers must then check client_kind
    instead of assuming which profile a token belongs to).
    """
    explicit = os.environ.get('HUB_CONFIG_FILE')
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute(): raise ValueError('HUB_CONFIG_FILE must be absolute')
        return _forbid_real_user_profile(path)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not set on Windows")
        base = Path(appdata)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return _forbid_real_user_profile(base / "agent-hub" / "cli.json")


def get_host_config_path() -> Path:
    """Legacy host profile path (same as get_config_path)."""
    return get_config_path()

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

    def __init__(self, base_url, path, starting_config, *, _load_fn=None, _save_fn=None):
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
        self._load_fn = _load_fn or load_config
        self._save_fn = _save_fn or save_config
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
        if self._load_fn() != self.starting_config:
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
        self._save_fn(cfg)

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

class ConfigPermissionError(PermissionError):
    """Safe diagnostic; never includes profile contents or arbitrary OS text."""


# Test hard guard (intake 6b624e65 AC2). host/tests hermetic isolation records
# where the real user profiles lived before the suite forced a temporary HOME;
# The bootstrap retains those roots in-process too: clearing or replacing
# os.environ in a test must not expose the original credential store.
TEST_GUARD_HOME_ENV = "HUB_TEST_GUARD_HOME"
TEST_GUARD_USER_HOME_ENV = "HUB_TEST_GUARD_USER_HOME"
TEST_GUARD_XDG_ENV = "HUB_TEST_GUARD_XDG_CONFIG_HOME"
TEST_GUARD_APPDATA_ENV = "HUB_TEST_GUARD_APPDATA"


def _test_guard_roots():
    roots = []
    for environment in (_test_isolation.guard_environment(), os.environ):
        guarded_home = environment.get(TEST_GUARD_HOME_ENV)
        if guarded_home:
            roots.append(Path(guarded_home) / ".config" / "agent-hub")
        user_home = environment.get(TEST_GUARD_USER_HOME_ENV)
        if user_home:
            roots.append(Path(user_home) / ".config" / "agent-hub")
        guarded_xdg = environment.get(TEST_GUARD_XDG_ENV)
        if guarded_xdg:
            roots.append(Path(guarded_xdg) / "agent-hub")
        guarded_appdata = environment.get(TEST_GUARD_APPDATA_ENV)
        if guarded_appdata:
            roots.append(Path(guarded_appdata) / "agent-hub")
    return roots


def _forbid_real_user_profile(path: Path) -> Path:
    if not (os.environ.get(TEST_GUARD_HOME_ENV)
            or _test_isolation.guard_environment().get(TEST_GUARD_HOME_ENV)):
        return path
    resolved = Path(path).resolve()
    for root in _test_guard_roots():
        root = root.resolve()
        if resolved == root or root in resolved.parents:
            raise ConfigPermissionError(
                'Hub test guard: refused to touch the real user profile during tests. '
                'Host tests are hermetic (host/tests scrub HOME/XDG before agent_hub_host '
                'is imported); use the isolated home or an explicit temporary path.')
    return path


def _has_symlink(path: Path) -> bool:
    is_junction = getattr(os.path, 'isjunction', lambda _: False)
    try:
        curr = Path(os.path.abspath(path))
        while True:
            if os.path.islink(curr) or is_junction(curr):
                return True
            parent = curr.parent
            if parent == curr:
                break
            curr = parent
    except OSError:
        return True
    return False


def _load_from(path: Path) -> dict:
    _forbid_real_user_profile(path)
    if _has_symlink(path):
        raise ConfigPermissionError(
            'Unsafe Hub profile permissions. On POSIX, the profile directory must be owned '
            'by your user with mode 700 and the profile file with mode 600. '
            'For the default profile run: chmod 700 ~/.config/agent-hub; '
            'chmod 600 ~/.config/agent-hub/cli.json (or host.json). '
            'Symlinks and multiply linked files are refused.')
    if not path.exists():
        return {}
    try:
        return _validate_data(json.loads(private_fs.read_bytes(path, 1024 * 1024)))
    except (PermissionError, NotADirectoryError):
        raise ConfigPermissionError(
            'Unsafe Hub profile permissions. On POSIX, the profile directory must be owned '
            'by your user with mode 700 and the profile file with mode 600. '
            'For the default profile run: chmod 700 ~/.config/agent-hub; '
            'chmod 600 ~/.config/agent-hub/cli.json (or host.json). '
            'Symlinks and multiply linked files are refused.') from None
    except OSError as exc:
        if getattr(exc, 'errno', None) in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise ConfigPermissionError(
                'Unsafe Hub profile permissions. On POSIX, the profile directory must be owned '
                'by your user with mode 700 and the profile file with mode 600. '
                'For the default profile run: chmod 700 ~/.config/agent-hub; '
                'chmod 600 ~/.config/agent-hub/cli.json (or host.json). '
                'Symlinks and multiply linked files are refused.') from None
        return {}
    except Exception:
        return {}


def _save_to(path: Path, data: dict, *, human_flow: bool = False):
    _forbid_real_user_profile(path)
    if not isinstance(data, dict):
        raise ValueError("Config data must be a dict")
    if is_human_config(_load_from(path)) and not (human_flow and is_human_config(data)):
        raise ConfigPermissionError(
            'Refusing to replace the human profile outside the human authentication flow.')
    cleaned = {}
    for k, v in data.items():
        if k == "refresh_token" and v is None:
            continue
        cleaned[k] = v
    if cleaned and _validate_data(cleaned) != cleaned:
        raise ValueError("Invalid config data")
    try:
        private_fs.atomic_write(path, json.dumps(cleaned).encode('utf-8'), 1024 * 1024)
    except Exception:
        raise RuntimeError('Config save failed') from None


def load_host_config() -> dict:
    """Legacy host profile only (host.json, or HUB_CONFIG_FILE override)."""
    return _load_from(get_host_config_path())


def save_host_config(data: dict):
    _save_to(get_host_config_path(), data)


def load_human_config() -> dict:
    """Human CLI profile only (cli.json, or HUB_CONFIG_FILE override).

    Never mistakes a legacy host token for human: callers must also check
    client_kind == "human" (see is_human_config).
    """
    return _load_from(get_human_config_path())


def save_human_config(data: dict):
    """Write the human profile, refusing hostile replacements (AC2).

    A valid human profile (client_kind human with a token) is never replaced
    by an empty token, a tokenless profile, or a host/agent credential from a
    different flow. Human logins and refreshes keep client_kind "human", so
    they pass; `hub auth logout` removes the file via clear_human_config().
    """
    path = get_human_config_path()
    _save_to(path, data, human_flow=True)


def clear_human_config():
    path = _forbid_real_user_profile(get_human_config_path())
    host_path = get_host_config_path()
    # In single-file override mode both paths coincide: removing it would
    # destroy the host profile too. Human logout in that mode only clears
    # human credentials when the file actually holds a human profile; a
    # host-kind file is left untouched and logout reports already-logged-out.
    if path == host_path:
        cfg = _load_from(path)
        if not is_human_config(cfg):
            return False
    if path.exists():
        path.unlink()
    return True


def is_human_config(cfg: dict) -> bool:
    return (
        isinstance(cfg, dict)
        and cfg.get("client_kind") == "human"
        and isinstance(cfg.get("access_token"), str)
        and bool(cfg.get("access_token"))
    )


def load_config() -> dict:
    """Active profile for normal user commands (human-first, host fallback).

    - HUB_CONFIG_FILE set: single-file mode, return that file as-is
      (legacy behavior; kind check left to callers).
    - Else: human cli.json when it holds an access token, otherwise
      legacy host.json. On fresh Linux after `hub auth login` only the
      human file exists, so sessions/intake/status automatically use it.
    - Host-only flows (hub login, host agent register/poll) must use
      load_host_config() explicitly so a human profile never overwrites
      or impersonates the host file.
    """
    if os.environ.get('HUB_CONFIG_FILE'):
        return _load_from(get_config_path())
    from .worker_auth import config_path
    worker = config_path()
    if worker is not None:
        return _load_from(worker)
    human = load_human_config()
    if isinstance(human.get("access_token"), str) and human.get("access_token"):
        return human
    return load_host_config()


def save_config(data: dict):
    """Legacy host save (host.json, or HUB_CONFIG_FILE override). Untouched."""
    from .worker_auth import config_path
    worker = None if os.environ.get('HUB_CONFIG_FILE') else config_path()
    _save_to(worker if worker is not None else get_config_path(), data)

def update_config(**kwargs):
    # Host-only (hub login / machine binding). Never reads the human
    # profile: a human login must not change host pairing, and host
    # registration must not mix human tokens into host.json.
    # AC2: resolve exactly ONE profile path up front (host.json, or the
    # HUB_CONFIG_FILE single-file override) and read and write only that
    # path - never the human cli.json and never a per-launch worker
    # profile discovered after the read.
    path = get_host_config_path()
    cfg = _load_from(path)
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
    _save_to(path, cfg)

def clear_config():
    path = _forbid_real_user_profile(get_config_path())
    if path.exists():
        path.unlink()

