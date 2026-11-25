"""Hub-owned Dapier CLI store and operator-side proactive device rotation.

POSIX hosts only. Never import or copy the host's Dapier login. Keep this
stdlib module identical in the maintained CLI and Hub2 host packages.
"""
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

IDENTITY = "orin@datatalks.club"
ORIGIN = "https://dapier.dtcdev.click"
RENEW_BEFORE = 7 * 86400
CHECK_INTERVAL = 3600
_LOCK = threading.Lock()
_THREAD = None


def shared_root():
    return Path(os.environ.get("HUB_SHARED_CLI_ROOT") or Path.home() / ".config/agent-hub/shared")


def private_write(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".dapier-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_session(root, now):
    path = Path(root) / "dapier/session.json"
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("dapier_store_not_private")
        if info.st_size > 32768:
            raise ValueError("dapier_session_invalid")
        value = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError("dapier_pairing_required") from None
    except (OSError, json.JSONDecodeError):
        raise ValueError("dapier_session_unreadable") from None
    if not isinstance(value, dict) or value.get("email") != IDENTITY:
        raise ValueError("dapier_identity_mismatch")
    if value.get("kind") != "device" or not isinstance(value.get("token"), str) or not value["token"].startswith("dapd_"):
        raise ValueError("dapier_device_session_required")
    if type(value.get("expires_at")) is not int or value["expires_at"] <= now:
        raise ValueError("dapier_pairing_required")
    return value


def _is_windows():
    return os.name == "nt" or sys.platform == "win32"


def session_environment(root, path=None):
    """Pin only dapier's XDG directory via a command wrapper, not other CLIs."""
    if _is_windows():
        return {}
    root = Path(root).resolve()
    directory = root / "bin"
    search = os.pathsep.join(p for p in (path or os.defpath).split(os.pathsep)
                             if p and Path(p).resolve() != directory)
    binary = shutil.which("dapier", path=search)
    if not binary:
        local_dapier = Path.home() / ".local/bin/dapier"
        binary = str(local_dapier) if local_dapier.is_file() else "/usr/bin/dapier"
    # A standalone stdlib wrapper survives host upgrades and never reads host
    # session.json, even for native profiles inheriting the operator's HOME.
    code = ("#!/usr/bin/env python3\nimport fcntl, json, os, sys, time\nfrom pathlib import Path\n"
            + "root = Path(" + repr(str(root)) + ")\n"
            + "try:\n    lock = open(root / '.dapier.lock', 'a')\n    os.chmod(lock.name, 0o600)\n    fcntl.flock(lock, fcntl.LOCK_EX)\n    os.set_inheritable(lock.fileno(), True)\n    p = root / 'dapier/session.json'\n    s = json.loads(p.read_text())\n"
            + "    assert not p.is_symlink() and not p.stat().st_mode & 0o077\n"
            + "    assert s.get('email') == " + repr(IDENTITY) + "\n"
            + "    assert s.get('kind') == 'device' and isinstance(s.get('token'), str) and s['token'].startswith('dapd_')\n"
            + "    assert type(s.get('expires_at')) is int and s['expires_at'] > time.time()\n"
            + "except Exception:\n    sys.exit('Hub Dapier credential unavailable; Developer must repair the shared Orin login.')\n"
            + "env = dict(os.environ, XDG_CONFIG_HOME=str(root))\n"
            + "os.execve(" + repr(binary) + ", [" + repr(binary) + ", *sys.argv[1:]], env)\n")
    wrapper = directory / "dapier"
    if not wrapper.is_file() or wrapper.read_text() != code:
        private_write(wrapper, code, mode=0o700)
    wrapper.chmod(0o700)
    return {"HUB_SHARED_CLI_ROOT": str(root), "PATH": str(directory) + os.pathsep + (path or os.defpath)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def rotate(token):
    request = urllib.request.Request(ORIGIN + "/api/agent/device/refresh", data=b"{}",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            value = json.loads(response.read(32769))
    except urllib.error.HTTPError as error:
        raise ValueError("dapier_refresh_rejected" if error.code in (401, 403) else "dapier_refresh_failed") from None
    except Exception:
        raise ValueError("dapier_refresh_failed") from None
    if (not isinstance(value, dict) or not isinstance(value.get("token"), str)
            or not value["token"].startswith("dapd_") or type(value.get("expires_at")) is not int
            or value["expires_at"] <= time.time() + RENEW_BEFORE):
        raise ValueError("dapier_refresh_invalid")
    return value


def notify(root, reason, episode):
    """Operator CLI owns the Developer mailbox path, never the machine token."""
    env = {k: v for k, v in os.environ.items() if k not in
           {"HUB_TOKEN", "HUB_JOB_TOKEN", "HUB_CONFIG_FILE", "HUB_PROFILE"}}
    env["HUB_CONFIG_FILE"] = str(Path(root).parent / "cli.json")
    body = ("Hub shared Dapier credential needs Developer repair: " + reason + ". "
            "Managed CLI identity must be orin@datatalks.club. "
            "Use the central shared-store pairing/rotation runbook; never the host login. "
            "Do not route to NEEDS_ALEXEY.")
    try:
        result = subprocess.run(["hub", "msg", "send", "--to", "agent:dev", "--key", "dapier-" + episode,
                                 "--body", body], env=env, capture_output=True, timeout=25, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("dapier_developer_alert_failed") from None
    if result.returncode:
        raise ValueError("dapier_developer_alert_failed")


def maintain(root=None, now=None, refresher=rotate, notifier=notify):
    """One serialized, durable hourly check; renew with seven days remaining.

    Failure returns a fixed slug and retries notification until acknowledged.
    Neither errors nor state files contain credential values or remote bodies.
    """
    if _is_windows():
        return "unsupported"
    import fcntl
    root = Path(root or shared_root()).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    now = time.time() if now is None else now
    with open(root / ".dapier.lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        state_path = root / "dapier-health.json"
        try:
            state = json.loads(state_path.read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        if now - state.get("checked_at", 0) < CHECK_INTERVAL:
            return state.get("status", "unknown")
        status = "valid"
        try:
            session = read_session(root, now)
            if session["expires_at"] - now <= RENEW_BEFORE:
                renewed = refresher(session["token"])
                if (not isinstance(renewed, dict) or not isinstance(renewed.get("token"), str)
                        or not renewed["token"].startswith("dapd_") or type(renewed.get("expires_at")) is not int
                        or renewed["expires_at"] <= now + RENEW_BEFORE):
                    raise ValueError("dapier_refresh_invalid")
                session.update(token=renewed["token"], expires_at=renewed["expires_at"], obtained_at=int(now))
                private_write(root / "dapier/session.json", json.dumps(session))
                status = "rotated"
        except ValueError as error:
            allowed = {"dapier_store_not_private", "dapier_session_invalid", "dapier_pairing_required",
                       "dapier_session_unreadable", "dapier_identity_mismatch", "dapier_device_session_required",
                       "dapier_refresh_rejected", "dapier_refresh_failed", "dapier_refresh_invalid"}
            status = str(error) if str(error) in allowed else "dapier_refresh_failed"
        except Exception:
            status = "dapier_store_failed"
        next_state = {"checked_at": now, "status": status}
        if status not in {"valid", "rotated"}:
            logging.error("Hub Dapier credential failure recipient=Developer reason=%s", status)
            episode = state.get("episode") or uuid.uuid4().hex
            next_state["episode"] = episode
            notified = state.get("notified", False)
            if not notified:
                try:
                    notifier(root, status, episode)
                    notified = True
                except Exception:
                    logging.error("Hub Dapier Developer alert delivery failed; will retry")
            next_state["notified"] = notified
        private_write(state_path, json.dumps(next_state))
        return status


def request_maintenance():
    """Bounded background work, independent of heartbeat and session starts."""
    global _THREAD
    if _is_windows():
        return
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        def run():
            try:
                maintain()
            except Exception:
                logging.error("Hub Dapier maintenance unavailable; Developer must inspect host")
        _THREAD = threading.Thread(target=run, name="hub-dapier", daemon=True)
        _THREAD.start()
