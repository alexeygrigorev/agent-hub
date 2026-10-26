"""Generic host placement client. Secrets stay in native clients, never subscription reports."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import uuid
import subprocess
import time
from typing import Any, Optional

from . import bus
from .session_rules import SESSION_PREAMBLE, compose_prompt
from .retired_native import verify_native_retirement

LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# Control characters the Hub server rejects in finish-report text
# (invalid_finish_report). The host strips them before forwarding so a
# worker-crafted result file can never wedge reconciliation with a 400.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control_chars(value: str) -> str:
    return _CONTROL_RE.sub("", value)
# Canonical quota services: `codex-orin` is not a service — it is the second
# Codex account (service `codex`, account `codex-orin`). The quse wire route
# key `codex-orin` is still collected by quota_snapshot; only this service
# namespace is canonical.
SERVICES = {"codex", "claude", "grok", "gemini", "zai", "go", "copilot"}
WORKSPACE_SEG = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")

# Delegated worker cgroup caps, applied to every Hub native start alongside
# --memory/--pids (intake 354c03f0, re-file of vanished 942473f7). One full
# CPU (quota 100000us per 100000us period) matches the aplexer default period;
# the installed aplexer Python Client hardcodes cpu_quota_us=None, so these
# flags travel the real `aplexer start` CLI path (client_call falls back to
# subprocess for flags outside its allowlist).
WORKER_MEMORY = "1500M"
WORKER_PIDS = "256"
WORKER_CPU_QUOTA_US = "100000"
WORKER_CPU_PERIOD_US = "100000"


def absolute_workspace_ok(value):
    """Bounded canonical absolute POSIX or Windows workspace identifier.

    Mirrors the Hub server validator: no NUL/control chars, no `//`, no
    trailing slash, no `.`/`..` segments, total length capped at 512.
    POSIX paths start with `/` and confine segments to `[A-Za-z0-9._-]{1,64}`.
    Windows paths start with a drive letter (e.g. `C:\\`), allow spaces in
    directory names, and forbid invalid/reserved Windows characters.
    """
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if '\x00' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    if value.startswith('/'):
        if '\\' in value:
            return False
        parts = value.split('/')
        if parts[0] != '' or len(parts) < 2:
            return False
        return all(seg and seg not in ('.', '..') and WORKSPACE_SEG.fullmatch(seg) for seg in parts[1:])
    # Windows drive path: C:\path or C:/path
    if len(value) >= 3 and value[1] == ':' and value[0].isascii() and value[0].isalpha() and value[2] in ('\\', '/'):
        if any(rep in value for rep in ('\\\\', '//', '\\/', '/\\')):
            return False
        if value.endswith(('\\', '/')):
            return False
        tail = value[3:]
        if not tail:
            return False
        parts = re.split(r'[\\/]', tail)
        for seg in parts:
            if not seg or seg in ('.', '..') or seg.endswith((' ', '.')):
                return False
            if any(ch in '<>"|?*:' or ord(ch) < 32 for ch in seg):
                return False
            base = seg.split('.')[0].upper()
            if base in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$',
                        *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
                return False
        return True
    return False




def _path_under_root(path: Path, root: Path) -> bool:
    """True when path is root or a real descendant (boundary-safe)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_roots(settings) -> list[str]:
    roots: list[str] = list(settings.get("workspace_roots") or [])
    ws = settings.get("workspaces") or {}
    for item, value in ws.items():
        for candidate in (item, value):
            if not isinstance(candidate, str) or not candidate:
                continue
            try:
                parent = Path(candidate)
                if not parent.is_absolute():
                    continue
                if parent.name == "worktrees" and absolute_workspace_ok(str(parent)):
                    roots.append(str(parent))
                elif parent.parent.name == "worktrees" and absolute_workspace_ok(str(parent.parent)):
                    roots.append(str(parent.parent))
            except Exception:
                continue
    seen: set[str] = set()
    out: list[str] = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        out.append(root)
    return out


def advertised_workspaces(settings) -> list[str]:
    """Hub capability workspace identifiers this host will accept.

    Includes configured map keys and absolute values, plus workspace_roots
    (and auto-detected `.../worktrees` parents). Children are NOT enumerated:
    machine registration caps JSON at 4 KiB, so Hub matches request paths by
    exact id or boundary-safe prefix under an advertised root (server-side).
    """
    ws = settings.get("workspaces") or {}
    out: set[str] = set()
    for key, value in ws.items():
        if isinstance(key, str) and key:
            out.add(key)
        if isinstance(value, str) and (value.startswith("/") or (len(value) >= 3 and value[1] == ":")):
            out.add(value)
    for root in _workspace_roots(settings):
        if absolute_workspace_ok(root):
            out.add(root)
    # Bare labels resolve under the first advertised absolute prefix. Prefer
    # operator-declared repository roots over a broad home mapping or legacy
    # per-repository aliases, including when the bounded catalog is truncated.
    roots = [root for root in _workspace_roots(settings) if absolute_workspace_ok(root)]
    # Keep the registration payload safely under the 4 KiB capabilities cap.
    return (roots + sorted(out - set(roots)))[:96]

def resolve_workspace_path(settings, name: str) -> Path:
    """Map an offer workspace id to a real local directory.

    Prefers the operator workspaces map. Absolute paths under configured or
    auto-detected worktrees roots are accepted as identity mappings so new git
    worktrees do not require editing subscriptions.json before Hub can place.
    Free-form paths outside those roots remain refused.
    """
    ws = settings.get("workspaces") or {}
    if name in ws:
        return Path(ws[name]).resolve(strict=True)
    if not isinstance(name, str) or not absolute_workspace_ok(name):
        raise ValueError("Local account/profile/workspace is not configured")
    path = Path(name)
    if not path.is_absolute():
        raise ValueError("Local account/profile/workspace is not configured")
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("Local account/profile/workspace is not configured") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("Local account/profile/workspace is not configured")
    ads = set(advertised_workspaces(settings))
    if name in ads or str(resolved) in ads:
        return resolved
    for root in _workspace_roots(settings):
        try:
            root_path = Path(root).resolve(strict=True)
        except Exception:
            continue
        if _path_under_root(resolved, root_path):
            return resolved
    raise ValueError("Local account/profile/workspace is not configured")


def validate_native_start_argv(value: Any) -> list[str]:
    """Optional trusted argv prefix for interactive native CLI starts (e.g. systemd-run)."""
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ValueError("native_start_argv must be a non-empty list of at most 64 arguments")
    for arg in value:
        if not isinstance(arg, str) or not arg or "\0" in arg or len(arg) > 1024:
            raise ValueError("native_start_argv elements must be non-empty strings without NUL")
    return value


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def subscriptions_path():
    base = Path(os.environ.get("APPDATA", "")) if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "agent-hub" / "subscriptions.json"


def validate_git_identity_config(conf):
    if not isinstance(conf, dict):
        raise ValueError("git identity setting must be a mapping")
    for k, v in conf.items():
        if not isinstance(k, str) or not k or len(k) > 128 or "\0" in k or "\n" in k:
            raise ValueError("Invalid git config key")
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                if not isinstance(sub_k, str) or not sub_k or len(sub_k) > 128 or "\0" in sub_k or "\n" in sub_k:
                    raise ValueError("Invalid git config subkey")
                if not isinstance(sub_v, (str, int, float, bool)) or len(str(sub_v)) > 1024 or "\0" in str(sub_v) or "\n" in str(sub_v):
                    raise ValueError("Invalid git config value")
        elif not isinstance(v, (str, int, float, bool)) or len(str(v)) > 1024 or "\0" in str(v) or "\n" in str(v):
            raise ValueError("Invalid git config value")


def normalize_git_identity(conf) -> dict[str, str]:
    if not isinstance(conf, dict):
        return {}
    res: dict[str, str] = {}
    for k, v in conf.items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                full_key = f"{k}.{sub_k}".lower()
                res[full_key] = str(sub_v)
        else:
            k_lower = k.lower()
            if k_lower in ("name", "user_name", "user.name"):
                res["user.name"] = str(v)
            elif k_lower in ("email", "user_email", "user.email"):
                res["user.email"] = str(v)
            elif k_lower in ("signing_key", "signingkey", "user.signingkey"):
                res["user.signingkey"] = str(v)
            elif k_lower in ("gpg_sign", "gpgsign", "commit.gpgsign"):
                res["commit.gpgsign"] = "true" if (v is True or str(v).lower() in ("true", "1", "yes")) else str(v)
            elif k_lower in ("push_default", "push.default"):
                res["push.default"] = str(v)
            elif k_lower in ("push_auto_setup_remote", "push.autosetupremote"):
                res["push.autosetupremote"] = "true" if (v is True or str(v).lower() in ("true", "1", "yes")) else str(v)
            elif "." in k:
                res[k] = str(v)
            else:
                res[f"user.{k}"] = str(v)
    return res


def load_settings(path=None):
    """Explicit non-secret machine configuration, including several accounts per service."""
    default_path = path is None
    path = Path(path) if path else subscriptions_path()
    if default_path and not path.exists():
        data = {'version': 1, 'subscriptions': [], 'workspaces': {'home': str(Path.home())}}
    else:
        if path.stat().st_size > 65536:
            raise ValueError("Subscription configuration exceeds 64 KiB")
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"version", "subscriptions", "workspaces", "workspace_roots", "launcher", "aplexer_bin", "capabilities", "max_agents", "cleanup_roots", "native_start_argv", "role_consumer", "role_config", "journal_dir", "git", "git_identity", "opencode_auth"} or data.get("version") != 1:
        raise ValueError("Invalid subscription configuration")
    if 'opencode_auth' in data:
        from .opencode_auth import validate
        validate(data['opencode_auth'])
    git_conf = data.get("git") if "git" in data else data.get("git_identity")
    if git_conf is not None:
        validate_git_identity_config(git_conf)
    if "journal_dir" in data:
        journal = data["journal_dir"]
        if not isinstance(journal, str) or not journal or "\0" in journal or not Path(journal).is_absolute():
            raise ValueError("journal_dir must be an absolute local path")
    if "native_start_argv" in data:
        validate_native_start_argv(data["native_start_argv"])
    if "role_consumer" in data:
        rc = data["role_consumer"]
        if not isinstance(rc, (bool, dict)):
            raise ValueError("Invalid role_consumer setting")
        if isinstance(rc, dict):
            if set(rc) - {"enabled", "interval_sec", "profiles"}:
                raise ValueError("Invalid role_consumer options")
            if "enabled" in rc and not isinstance(rc["enabled"], bool):
                raise ValueError("role_consumer.enabled must be a boolean")
            if "interval_sec" in rc and (not isinstance(rc["interval_sec"], (int, float)) or rc["interval_sec"] <= 0):
                raise ValueError("role_consumer.interval_sec must be positive number")
            if "profiles" in rc and (not isinstance(rc["profiles"], list) or any(not isinstance(p, str) for p in rc["profiles"])):
                raise ValueError("role_consumer.profiles must be a list of strings")
    if "role_config" in data:
        rc_cfg = data["role_config"]
        if not isinstance(rc_cfg, (str, dict)):
            raise ValueError("Invalid role_config setting")
        if isinstance(rc_cfg, str) and (not rc_cfg or "\0" in rc_cfg or len(rc_cfg) > 1024):
            raise ValueError("Invalid role_config path")
    subs = data.get("subscriptions")
    if not isinstance(subs, list) or len(subs) > 128:
        raise ValueError("Invalid subscriptions list")
    seen = set()
    for sub in subs:
        if not isinstance(sub, dict) or set(sub) - {"service", "account_label", "plan", "engine", "profile", "quota_argv", "admission_argv", "launcher_provider", "launcher_model", "launcher_effort", "startup_argv", "native_start_argv"}:
            raise ValueError("Subscription accepts metadata and local command references only")
        if "native_start_argv" in sub:
            validate_native_start_argv(sub["native_start_argv"])
        if sub.get("service") not in SERVICES or not isinstance(sub.get("account_label"), str) or not LABEL.fullmatch(sub["account_label"]) or sub["account_label"] in seen:
            raise ValueError("Invalid or duplicate account label")
        seen.add(sub["account_label"])
        for key in ("engine", "profile"):
            if sub.get(key) is not None and (not isinstance(sub[key], str) or not LABEL.fullmatch(sub[key])):
                raise ValueError("Invalid engine or profile label")
        plan = sub.get("plan")
        if plan is not None and (not isinstance(plan, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}", plan)):
            raise ValueError("Invalid plan label")
        for key in ("quota_argv", "admission_argv"):
            if key in sub and (not isinstance(sub[key], list) or not sub[key] or any(not isinstance(x, str) or not x or "\0" in x for x in sub[key])):
                raise ValueError("Local probes require an argv list")
    workspaces = data.get("workspaces", {})
    # Keys are either legacy labels or explicitly allowed absolute POSIX
    # paths (bounded canonical form, same rules as the Hub server). Every key
    # — label or absolute — maps to an absolute local directory through this
    # operator-configured mapping only: an offer workspace not present here is
    # refused before any claim or native effect. Absolute keys let a remote
    # caller address `/absolute/repo/path` directly while the host still
    # executes only explicitly configured directories (host realpath/CWD
    # confinement stays authoritative downstream).
    if not isinstance(workspaces, dict):
        raise ValueError("Workspaces must map labels or absolute paths to absolute local paths")
    for key, value in workspaces.items():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Workspaces must map labels or absolute paths to absolute local paths")
        if not isinstance(key, str) or not key:
            raise ValueError("Workspaces must map labels or absolute paths to absolute local paths")
        if LABEL.fullmatch(key):
            continue
        if not absolute_workspace_ok(key):
            raise ValueError("Workspaces must map labels or absolute paths to absolute local paths")
    workspace_roots = data.get("workspace_roots", [])
    if not isinstance(workspace_roots, list) or len(workspace_roots) > 32:
        raise ValueError("Invalid workspace_roots allowlist")
    cleaned_roots = []
    for root in workspace_roots:
        if not isinstance(root, str) or not absolute_workspace_ok(root):
            raise ValueError("Invalid workspace_roots allowlist")
        cleaned_roots.append(root)
    if len(set(cleaned_roots)) != len(cleaned_roots):
        raise ValueError("Invalid workspace_roots allowlist")
    data["workspace_roots"] = cleaned_roots
    needs = data.get('capabilities', [])
    if not isinstance(needs, list) or len(needs) > 64 or any(not isinstance(n, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:_-]{0,95}', n) for n in needs) or len(set(needs)) != len(needs):
        raise ValueError('Invalid task capabilities')
    maximum = data.get('max_agents', 1)
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError('Invalid machine concurrency')
    # M15d trusted cleanup-root allowlist (plumbing only): explicit absolute
    # host paths under which owned per-session worktrees may be removed after
    # confirmed stop. Validated strictly at use time by
    # session_cleanup.canonical_roots; the Fleet dispatcher never reads it.
    roots = data.get('cleanup_roots', [])
    if not isinstance(roots, list) or len(roots) > 16 or any(
            not isinstance(r, str) or not r or '\x00' in r or not (r.startswith('/') or (len(r) >= 3 and r[1] == ':' and r[0].isascii() and r[0].isalpha() and r[2] in ('\\', '/'))) for r in roots):
        raise ValueError('Invalid cleanup roots allowlist')
    if len(set(roots)) != len(roots):
        raise ValueError('Invalid cleanup roots allowlist')
    from .provisioning import managed_subscriptions
    managed = managed_subscriptions()
    # Managed state is the authoritative installed revision for that account,
    # exactly as in Dispatcher.once(). Canonical subscriptions may retain its
    # local declaration; restarting must not require a reduced recovery file
    # that accidentally drops unrelated native subscriptions and workspaces.
    managed_labels = {s['account_label'] for s in managed}
    data['subscriptions'] = [s for s in data['subscriptions'] if s['account_label'] not in managed_labels] + managed
    return data


def run_json(argv, *, payload=None, cwd=None, timeout=30):
    """No shell, no provider stdout/stderr in exceptions or logs."""
    from .aplexer_client import client_call
    native = client_call(argv[0], argv[1:]) if payload is None else None
    if native is not None:
        if native.returncode:
            raise RuntimeError("Native client operation failed; reconcile effects")
        if len(native.stdout.encode("utf-8")) > 8 * 1024 * 1024:
            raise RuntimeError("Native client response exceeds bound; reconcile effects")
        return json.loads(native.stdout)
    import threading
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    overflow = threading.Event()
    stdout = bytearray()
    stderr = bytearray()
    def drain(stream, limit, target=None):
        size = 0
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk: break
                size += len(chunk)
                if size > limit:
                    overflow.set(); break
                if target is not None: target.extend(chunk)
        except Exception:
            overflow.set()
        finally:
            stream.close()
    def write():
        try:
            if payload is not None: proc.stdin.write(json.dumps(payload).encode('utf-8'))
            proc.stdin.close()
        except Exception:
            overflow.set()
    # The native catalog includes containment and process metadata per session.
    # Keep provider probes small while allowing the bounded 500-session catalog.
    is_catalog = list(argv[-2:]) == ["list", "--json"] or list(argv[-3:]) == ["list", "--all", "--json"]
    stdout_limit = 8 * 1024 * 1024 if is_catalog else 131072
    threads = [threading.Thread(target=drain, args=(proc.stdout, stdout_limit, stdout), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 16384, stderr), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for thread in threads: thread.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None and not overflow.is_set() and time.monotonic() < deadline:
            overflow.wait(.02)
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=2)
            raise RuntimeError('Local command failed or exceeded resource limit')
        for thread in threads: thread.join(timeout=1)
        if proc.returncode != 0 or overflow.is_set() or any(t.is_alive() for t in threads):
            from .launch_diagnostics import LocalCommandError
            raise LocalCommandError(bytes(stdout + b'\n' + stderr).decode('utf-8', 'replace'), exit_code=proc.returncode)
    finally:
        if proc.poll() is None: proc.kill(); proc.wait(timeout=2)
        # Each reader owns and closes its stream after EOF. Closing its raw descriptor here
        # races with BufferedReader cleanup and can close an unrelated later probe's descriptor.
        for thread in threads: thread.join(timeout=1)
    try:
        value = json.loads(stdout)
    except Exception:
        raise RuntimeError("Local command returned invalid JSON") from None
    if not isinstance(value, (dict, list)):
        raise RuntimeError("Local command returned invalid JSON shape")
    return value


def quota_metadata(raw, service, profile=None):
    """Reuse the maintained quota contract; never reimplement its failure/absence semantics."""
    from .quota_snapshot import sanitize_provider, format_iso_utc, COLLECTOR_VERSION_REGEX
    lookup = profile if (isinstance(raw, dict) and profile and profile in raw) else service
    route = raw.get(lookup, raw.get(service, raw)) if isinstance(raw, dict) else {}
    if not isinstance(route, dict): route = {}
    details = route.get('details') if isinstance(route.get('details'), dict) else {}
    now = dt.datetime.now(dt.timezone.utc)
    clean = sanitize_provider(service, route, now)
    status = 'ok' if clean['status'] == 'ok' else 'unknown'
    explicit_timestamp = 'observed_at' in route or 'observed_at' in details
    if explicit_timestamp and (route.get('observed_at') is None and details.get('observed_at') is None):
        status = 'unknown'
        observed_at = None
    else:
        observed_at = clean['observed_at'] if explicit_timestamp else format_iso_utc(now)
    if explicit_timestamp and observed_at is None: status = 'unknown'
    # Extra conservative handling of explicitly unknown freshness and optional stale flags.
    if any('stale' in obj and obj['stale'] is not False for obj in (route, details)):
        status = 'unknown'
    windows = route.get('windows')
    if isinstance(windows, dict) and any(isinstance(w, dict) and 'stale' in w and w['stale'] is not False for w in windows.values()):
        status = 'unknown'
    window_keys = ('present', 'percent_remaining', 'reset_at', 'rolling', 'error',
                   'credits_remaining', 'spend_used', 'spend_limit', 'unit', 'profile')
    quota = {'status': status, 'observed_at': observed_at, 'details': clean['details'],
             'windows': {name: {k: w.get(k) for k in window_keys} for name, w in clean['windows'].items()}}
    collector_version = raw.get('collector_version') if isinstance(raw, dict) else None
    quota['collector_version'] = collector_version if isinstance(collector_version, str) and COLLECTOR_VERSION_REGEX.match(collector_version) else None
    return quota


def codex_plan():
    """Read local app-server account metadata only; never initiate login, token refresh or a turn."""
    import queue
    import threading
    replies = queue.Queue(maxsize=16)
    proc = None
    try:
        proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        def read():
            try:
                for _ in range(32):
                    line = proc.stdout.readline(65537)
                    if not line or len(line) > 65536: break
                    value = json.loads(line)
                    if value.get('id') in (1, 2): replies.put_nowait(value)
            except Exception:
                pass
        reader = threading.Thread(target=read, daemon=True); reader.start()
        def send(value):
            proc.stdin.write(json.dumps(value) + '\n'); proc.stdin.flush()
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "hub-local-metadata", "version": "1"}}})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            reply = replies.get(timeout=max(.01, deadline - time.monotonic()))
            if reply.get('id') == 1:
                if 'error' in reply: return None
                send({"method": "initialized"})
                send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
            elif reply.get('id') == 2:
                account = reply.get('result', {}).get('account') or {}
                plan = account.get('planType')
                return plan if plan in {'free', 'plus', 'pro', 'team', 'business', 'enterprise', 'edu'} else None
    except Exception:
        pass
    finally:
        if proc:
            try:
                proc.stdin.close(); proc.terminate(); proc.wait(timeout=2)
            except Exception:
                proc.kill(); proc.wait(timeout=2)
            if proc.stdout: proc.stdout.close()
    return None


def collect_subscriptions(settings, runner=run_json):
    """Probe default accounts once per service. Alternate accounts require explicit wrappers."""
    subscriptions = settings["subscriptions"]
    counts = {service: sum(s["service"] == service for s in subscriptions) for service in SERVICES}
    reports = []
    for sub in subscriptions:
        service = sub["service"]
        raw = {}
        profile = sub.get("profile")
        # quse itself uses existing native authentication. We never open credential files.
        command = sub.get("quota_argv") or (["quse", service, "--json"] if counts[service] == 1 and sub.get("profile") is None else None)
        if command:
            try:
                raw = runner(command)
            except Exception:
                pass
        plan = sub.get("plan")
        if plan is None and service == "codex" and counts[service] == 1 and sub.get("profile") is None:
            plan = codex_plan()
        # Only this documented status command is used for automatic tier metadata.
        # Account identity is explicitly configured; never emit email/org IDs or hash credentials.
        if plan is None and service == "claude" and counts[service] == 1 and sub.get("profile") is None:
            try:
                detected = runner(["claude", "auth", "status", "--json"]).get("subscriptionType")
                if detected in {"free", "pro", "max", "team", "enterprise"}:
                    plan = detected
            except Exception:
                pass
        if plan is None and service in {"claude", "grok"}:
            route = raw.get(service, raw) if isinstance(raw, dict) else {}
            details = route.get("details") if isinstance(route, dict) else None
            value = details.get("subscription") if isinstance(details, dict) else None
            if value in {"free", "pro", "max", "team", "enterprise", "SuperGrok", "SuperGrokPlus", "SuperGrokHeavy"}:
                plan = value
        reports.append({"service": service, "account": sub["account_label"], "plan": plan, "quota": quota_metadata(raw, service, profile=profile)})
    return reports


def resolve_offer_cwd(workspace: Path, cwd: object) -> Path:
    """Resolve a hub launch offer cwd against the workspace root.

    ``None`` (or ``'.'``) means the workspace root itself. Anything else must
    be a workspace-relative confined path; it is resolved with symlink
    confinement and must stay inside the workspace root, else the offer is
    refused with no native effect. The resolved absolute path is what reaches
    the native start adapter and what catalog verification compares.
    """
    if cwd is None or cwd == '.':
        return workspace
    if not isinstance(cwd, str) or not cwd or len(cwd) > 256 or cwd.startswith('/') \
            or '\\' in cwd or ':' in cwd or '\x00' in cwd:
        raise ValueError('Offer cwd is not a workspace-relative path')
    parts = cwd.split('/')
    if any(not seg or seg in ('.', '..') or len(seg) > 64 or not all(c.isalnum() or c in '._-' for c in seg) for seg in parts):
        raise ValueError('Offer cwd escapes or is malformed')
    candidate = (workspace.joinpath(*parts)).resolve(strict=False)
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise ValueError('Offer cwd escapes the workspace root') from None
    return candidate


def _ms_to_iso(value):
    """aplexer reports epoch milliseconds; anything else is unknown rather than guessed."""
    if type(value) is not int or value <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def session_metadata(raw, settings, progress=None):
    from .account_capacity import confirmed_empty
    from .session_progress import event_ms
    sessions = raw if isinstance(raw, list) else raw.get("sessions", [])
    clean = []
    for session in sessions:
        engine, profile = session.get("engine"), session.get("profile")
        matches = [s for s in settings["subscriptions"] if s.get("engine", s["service"]) == engine and s.get("profile") == profile]
        created_at_ms = session.get("created_at_ms") or session.get("created_ms")
        if isinstance(created_at_ms, str) and created_at_ms.isdigit():
            created_at_ms = int(created_at_ms)
        elif not isinstance(created_at_ms, int) or created_at_ms <= 0:
            created_at_ms = None
        ws = session.get("workspace", "")
        cwd = session.get("cwd") if isinstance(session.get("cwd"), str) else None
        phase = session.get("state") or session.get("phase", "unknown")
        exit_confirmed = confirmed_empty(session)
        # Kernel annotations affect path spelling, never lifecycle proof. Keep
        # real existing directories whose literal names end with the suffix.
        if isinstance(ws, str) and ws.endswith(' (deleted)') and not os.path.exists(ws):
            ws = ws.removesuffix(' (deleted)')
        if isinstance(cwd, str) and cwd.endswith(' (deleted)') and not os.path.exists(cwd):
            cwd = cwd.removesuffix(' (deleted)')
        if exit_confirmed:
            phase = 'exited'
        elif phase in ('exited', 'dead', 'pruned', 'forgotten'):
            phase = 'uncertain'
        clean.append({"native_id": session["id"], "tag": session["tag"], "workspace": ws, "engine": engine,
                      "profile": profile, "account": matches[0]["account_label"] if len(matches) == 1 else None,
                      "phase": phase, "confirmed_exit": exit_confirmed, "agent_state": session.get("reported_state"),
                      "cwd": cwd,
                      "last_activity_at": _ms_to_iso(event_ms(session)),
                      "created_at_ms": created_at_ms,
                      **({'progress': progress[session['id']]} if progress and session['id'] in progress else {})})
    return clean


def binding_match_tags(fleet_launch_id):
    """Exact native tags a Fleet-owned start may carry.

    Interactive starts use ``hub-<id>`` (the exact Dispatcher argv tag).
    Maintained-launcher aplexer-backend starts use ``task-hub-<id>``: the
    launcher tags task ``hub-<id>`` through run_tag_for, which prefixes the
    ``task-`` namespace. Both are canonical expected tags, never guesses.
    """
    return ("hub-" + fleet_launch_id, "task-hub-" + fleet_launch_id)


def find_binding_row(rows, entry):
    """Match one pending host-binding entry to its observed native row.

    Binds the exact canonical server launch native_id to the actual native
    row (UUID + engine + workspace + creation), never tag-only: when the
    pending entry carries a canonical ``native_id`` (server seam), the row
    id must equal it exactly; otherwise exactly one row must carry an
    expected tag with a matching engine. Rows without a workspace or
    without integer native creation evidence are never bound (the server
    refuses them, and a UUID without incarnation proves nothing). Returns
    the row dict or None when there is no exact single match.
    """
    if not isinstance(rows, list) or not isinstance(entry, dict):
        return None
    launch_id = entry.get("fleet_launch_id")
    if not isinstance(launch_id, str) or not launch_id:
        return None
    expected = set(binding_match_tags(launch_id))
    engine = entry.get("engine")
    candidates = [r for r in rows if isinstance(r, dict) and r.get("tag") in expected and r.get("engine") == engine]
    known = entry.get("native_id")
    if isinstance(known, str) and known:
        exact = [r for r in candidates if isinstance(r.get("id"), str) and r["id"].lower() == known.lower()]
        if len(exact) != 1:
            return None
        candidates = exact
    elif len(candidates) != 1:
        return None
    row = candidates[0]
    if not isinstance(row.get("id"), str) or not row["id"]:
        return None
    if not isinstance(row.get("workspace"), str) or not row["workspace"]:
        return None
    if type(row.get("created_at_ms")) is not int or row["created_at_ms"] <= 0:
        return None
    return row


class Dispatcher:
    """One finite poll. Claim ambiguity and effect ambiguity are never retried automatically."""
    def __init__(self, settings, request=bus._do_request, runner=run_json, role_config=None, role_config_path=None, journal_dir=None):
        self.settings, self.request, self.runner = settings, request, runner
        from .provisioning import managed_subscriptions
        self._managed_labels = {s['account_label'] for s in managed_subscriptions()}
        self.role_config = role_config
        self.role_config_path = role_config_path
        self.journal_dir = journal_dir or settings.get("journal_dir")
        self._imported_sessions_cursor: Optional[str] = None
        self._active_launches: dict[str, dict[str, Any]] = {}
        if self.journal_dir:
            self._load_pending_launches_from_journal()

    @staticmethod
    def _valid_imported_cursor(cursor) -> bool:
        return (isinstance(cursor, str) and bool(cursor)
                and len(cursor.encode("utf-8")) <= 512
                and not any(ord(c) < 32 or ord(c) == 127 for c in cursor))

    def _get_imported_cursor(self) -> Optional[str]:
        if self.journal_dir:
            from . import private_fs
            try:
                raw = private_fs.read_bytes(Path(self.journal_dir) / "imported-cursor.json", 4096)
                data = json.loads(raw.decode("utf-8"))
                cursor = data.get("cursor") if isinstance(data, dict) else None
                return cursor if self._valid_imported_cursor(cursor) else None
            except FileNotFoundError:
                pass
            except Exception:
                # Refused, oversized or malformed files never supply a cursor.
                return None
        cursor = getattr(self, "_imported_sessions_cursor", None)
        return cursor if self._valid_imported_cursor(cursor) else None

    def _set_imported_cursor(self, cursor: Optional[str]) -> None:
        if cursor is not None and not self._valid_imported_cursor(cursor):
            raise ValueError("Invalid imported cursor")
        self._imported_sessions_cursor = cursor
        if self.journal_dir:
            from . import private_fs
            try:
                raw = json.dumps({"cursor": cursor}).encode("utf-8")
                private_fs.atomic_write(Path(self.journal_dir) / "imported-cursor.json", raw, 4096)
            except Exception:
                # Keep safe instance progress; retry after restart is idempotent.
                # A private-file refusal must never trigger a weaker writer.
                pass

    def _journal_launches_dir(self) -> Optional[Path]:
        if not self.journal_dir:
            return None
        p = Path(self.journal_dir) / "fleet-launches"
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p

    def _persist_launch_record(self, record: dict[str, Any]) -> None:
        jdir = self._journal_launches_dir()
        if not jdir:
            return
        offer_id = record.get("offer_id")
        if not isinstance(offer_id, str) or not LABEL.fullmatch(offer_id):
            return
        entry_file = jdir / f"{offer_id}.json"
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        from . import private_fs
        private_fs.atomic_write(str(entry_file), raw, 65536)

    def _load_pending_launches_from_journal(self) -> None:
        jdir = self._journal_launches_dir()
        if not jdir or not jdir.is_dir():
            return
        from .native_sessions import NativeTarget, SessionControlError
        from . import private_fs
        for entry in sorted(jdir.iterdir()):
            if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                if entry.stat().st_size > 65536 or entry.stat().st_size <= 0:
                    continue
                raw = private_fs.read_bytes(str(entry), 65536, private=True).decode("utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
                if data.get("settled") is True:
                    continue
                if data.get("state") == "not_started":
                    offer_id = data.get("offer_id")
                    if (isinstance(offer_id, str) and LABEL.fullmatch(offer_id)
                            and entry.name == f"{offer_id}.json"
                            and data.get("task_id") == f"hub-{offer_id}"
                            and data.get("mode") == "headless"
                            and isinstance(data.get("reason"), str)
                            and data["reason"].startswith("launcher_")):
                        self._active_launches[offer_id] = data
                    continue
                if data.get("state") != "running":
                    continue
                offer_id = data.get("offer_id")
                task_id = data.get("task_id")
                native_id = data.get("native_id")
                tag = data.get("tag")
                workspace = data.get("workspace")
                engine = data.get("engine")
                incarnation = data.get("incarnation")
                if not (isinstance(offer_id, str) and LABEL.fullmatch(offer_id)):
                    continue
                if not (isinstance(task_id, str) and LABEL.fullmatch(task_id)):
                    continue
                if (data.get("mode") != "headless" or task_id != f"hub-{offer_id}"
                        or tag != f"task-{task_id}" or entry.name != f"{offer_id}.json"):
                    continue
                if not (isinstance(native_id, str) and native_id):
                    continue
                if not (isinstance(tag, str) and tag):
                    continue
                if not (isinstance(workspace, str) and Path(workspace).is_absolute()):
                    continue
                if not (isinstance(engine, str) and engine):
                    continue
                if type(incarnation) is not int or incarnation <= 0:
                    continue
                NativeTarget(
                    session_id=native_id, tag=tag, workspace=workspace,
                    created_at_ms=incarnation, engine=engine
                ).validated()
                self._active_launches[offer_id] = data
            except Exception:
                continue

    def _read_hub_finish_report(self, record: dict[str, Any]) -> dict[str, Any]:
        """Read the worker's .local/hub-results/<offer_id>.json for intake ingestion.

        Intake 88b3260d AC1: the host forwards the sanitized final result
        (status/report/artifacts) inside the terminal Fleet result payload so
        the server can append it to the linked intake. The result path must be
        exactly <workspace>/.local/hub-results/<offer_id>.json (no symlinks,
        no `..` escapes; the path is host-constructed at dispatch). Fields are
        bounded and control-char-stripped to match the server's
        invalid_finish_report validation, so a worker-crafted file can never
        wedge reconciliation with a 400. Never logs file contents. Returns {}
        when absent or invalid (fail-open to a bare terminal receipt).
        """
        try:
            offer_id = record.get("offer_id")
            result_path = record.get("result_path")
            workspace = record.get("workspace")
            if not isinstance(offer_id, str) or not LABEL.fullmatch(offer_id):
                return {}
            if not isinstance(result_path, str) or not isinstance(workspace, str):
                return {}
            import os as _os
            expected = _os.path.join(_os.path.abspath(workspace), ".local", "hub-results", offer_id + ".json")
            if _os.path.abspath(result_path) != expected:
                return {}
            rp = Path(expected)
            if rp.is_symlink():
                return {}
            if not rp.is_file():
                return {}
            try:
                if rp.stat().st_size <= 0 or rp.stat().st_size > 65536:
                    return {}
            except OSError:
                return {}
            from . import private_fs
            # Worker-authored and world-readable under its own umask; confine
            # by the exact-path check above, not by privacy bits.
            raw = private_fs.read_bytes(str(rp), 65536, private=False).decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            status = data.get("status")
            if status not in ("done", "failed", "waiting-on-human", "stuck"):
                return {}
            report = data.get("report")
            if not isinstance(report, str):
                return {}
            report = _strip_control_chars(report.strip())
            if len(report) > 4096:
                report = report[:4096]
            if _CONTROL_RE.search(report):
                return {}
            artifacts = data.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []
            clean_artifacts = []
            for item in artifacts[:8]:
                if not isinstance(item, dict):
                    continue
                label_text = item.get("label")
                text = item.get("text")
                if not isinstance(label_text, str) or not isinstance(text, str):
                    continue
                label_text = _strip_control_chars(label_text)[:120]
                text = _strip_control_chars(text)[:1024]
                if _CONTROL_RE.search(label_text) or _CONTROL_RE.search(text):
                    continue
                clean_artifacts.append({"label": label_text, "text": text})
            return {"report_status": status, "report": report, "artifacts": clean_artifacts}
        except Exception:
            return {}

    def _record_running_launch(self, offer_id, task_id, workspace, engine, account, native_id, result_path, details, profile=None, launched_at_ts=None, log_offsets=None):
        from .native_sessions import NativeTarget, SessionControlError
        from . import private_fs

        try:
            norm_uuid = str(uuid.UUID(str(native_id))).lower()
        except (ValueError, TypeError, AttributeError):
            return

        tag = details.get("tag")
        expected_tag = f"task-{task_id}" if not task_id.startswith("task-") else task_id
        if not tag or not isinstance(tag, str):
            tag = expected_tag

        incarnation = details.get("created_at_ms") or details.get("incarnation")
        if type(incarnation) is not int or incarnation <= 0:
            start_path = workspace / ".local" / f"launch-{task_id}-start.json"
            if not start_path.is_symlink() and start_path.is_file():
                try:
                    if 0 < start_path.stat().st_size <= 65536:
                        raw = private_fs.read_bytes(str(start_path), 65536, private=False).decode("utf-8")
                        s_doc = json.loads(raw)
                        if isinstance(s_doc, dict):
                            s_data = s_doc.get("wrapper_start_json") if isinstance(s_doc.get("wrapper_start_json"), dict) else s_doc
                            cand_inc = s_data.get("created_at_ms")
                            if type(cand_inc) is int and cand_inc > 0:
                                incarnation = cand_inc
                            if not details.get("tag") and s_data.get("tag"):
                                tag = s_data["tag"]
                except Exception:
                    pass

        if type(incarnation) is not int or incarnation <= 0:
            return

        try:
            target = NativeTarget(
                session_id=norm_uuid,
                tag=tag,
                workspace=str(workspace.resolve(strict=True)),
                created_at_ms=incarnation,
                engine=engine,
            ).validated()
        except (SessionControlError, ValueError, OSError):
            return

        record = {
            "offer_id": offer_id,
            "task_id": task_id,
            "mode": "headless",
            "workspace": target.workspace,
            "engine": target.engine,
            "account": account,
            "profile": profile,
            "native_id": target.session_id,
            "tag": target.tag,
            "incarnation": target.created_at_ms,
            "result_path": str(result_path),
            "state": "running",
            "settled": False,
            "dispatched_at": utcnow(),
            "launched_at_ts": launched_at_ts,
            "log_offsets": log_offsets,
        }
        self._active_launches[offer_id] = record
        self._persist_launch_record(record)

    def _check_headless_health(self, offer_id, task_id, workspace, engine):
        """Fail only this host-owned launch; never release capacity on refusal alone."""
        from . import headless_health
        from .native_sessions import stop_session
        try:
            target = headless_health.launch_target(workspace, task_id, engine)
            prior = self._active_launches.get(offer_id)
            if prior and prior.get('native_id') and any((
                    prior.get('native_id') != target.session_id, prior.get('tag') != target.tag,
                    prior.get('incarnation') != target.created_at_ms,
                    prior.get('workspace') != target.workspace, prior.get('engine') != target.engine)):
                return None
            diagnostic = headless_health.inspect(self.settings.get('aplexer_bin', 'aplexer'), target)
        except Exception:
            return None
        if diagnostic is None:
            return None
        # Hold before cleanup: native spawn/admission never proved tool health.
        self.request('POST', '/api/fleet/probe', {'engine': engine, 'status': 'unavailable',
                                                'reason': diagnostic['reason']})
        record = self._active_launches.get(offer_id)
        if record is None or not record.get('native_id'):
            self._record_running_launch(offer_id, task_id, Path(workspace), engine, None,
                                       target.session_id, Path(workspace) / '.local' / 'hub-results' / (offer_id + '.json'),
                                       {'tag': target.tag, 'created_at_ms': target.created_at_ms})
            record = self._active_launches.get(offer_id)
        if record is None:
            return None
        record['diagnostic'] = diagnostic
        self._persist_launch_record(record)
        confirmed = False
        try:
            # A single kill attempt. If its effect is uncertain, persist its
            # oracle and reconcile read-only on the next cycle.
            if record.get('health_stop_oracle'):
                from .native_sessions import reconcile_stop_oracle
                confirmed, _ = reconcile_stop_oracle(self.settings.get('aplexer_bin', 'aplexer'),
                                                     record['health_stop_oracle'], record.get('health_stop_kill', {}))
            elif not record.get('health_stop_attempted'):
                record['health_stop_attempted'] = True
                self._persist_launch_record(record)
                outcome = stop_session(self.settings.get('aplexer_bin', 'aplexer'), target)
                confirmed = outcome.state == 'stopped'
        except Exception as exc:
            oracle = getattr(exc, 'oracle', None)
            if isinstance(oracle, dict):
                record['health_stop_oracle'] = oracle
                record['health_stop_kill'] = getattr(exc, 'kill', {})
        payload = {'state': 'failed' if confirmed else 'uncertain', 'native_id': target.session_id,
                   'launcher_task_id': task_id, 'diagnostic': diagnostic}
        if confirmed:
            payload['confirmed_exit'] = True
        ack = self.request('POST', '/api/fleet/launches/' + offer_id + '/result', payload)
        if confirmed and isinstance(ack, dict) and ack.get('id') == offer_id and ack.get('state') == 'failed':
            record['state'], record['settled'] = 'failed', True
        self._persist_launch_record(record)
        return {'id': offer_id, **payload, 'reason': diagnostic['reason']}

    def reconcile_launches(self) -> list[dict[str, Any]]:
        """Reconcile pending owned headless launches via verified positive retirement,
        Launcher reconcile, and own-host Fleet confirmed-exit result.
        """
        reconciled = []
        if not self._active_launches:
            return reconciled

        from .native_sessions import NativeTarget, SessionControlError

        native = self.settings.get("aplexer_bin", "aplexer")
        state_dir = self.settings.get("aplexer_state_dir")
        launcher = self.settings.get("launcher", {})
        has_launcher = all(launcher.get(k) for k in ["argv", "root", "config_dir"])

        for offer_id, record in list(self._active_launches.items()):
            if record.get("settled") is True:
                continue
            if record.get("state") == "not_started":
                try:
                    # Repeat the fenced proof, including after process restart.
                    # Never treat a journal assertion alone as native exit proof.
                    from .launcher_recovery import settle_unstarted
                    if not settle_unstarted(launcher, record["task_id"], record["workspace"], record["target_cwd"], record.get("diagnostic")):
                        continue
                    self.request("POST", "/api/fleet/launches/" + offer_id + "/result", {
                        "state": "failed", "launcher_task_id": record["task_id"], "confirmed_exit": True, "reason": record["reason"],
                        **({"diagnostic": record["diagnostic"]} if record.get("diagnostic") else {})})
                    record["settled"] = True
                    self._persist_launch_record(record)
                    reconciled.append({"id": offer_id, "state": "failed", "reason": record["reason"],
                                       **({"diagnostic": record["diagnostic"]} if record.get("diagnostic") else {})})
                except Exception:
                    pass
                continue
            if (record.get("mode") != "headless" or record.get("offer_id") != offer_id
                    or record.get("task_id") != f"hub-{offer_id}"
                    or record.get("tag") != f"task-hub-{offer_id}"):
                continue

            # 1. Corroborate exact 5-field native tuple
            incarnation = record.get("incarnation")
            if not isinstance(incarnation, int) or incarnation <= 0:
                continue
            engine = record.get("engine")
            if not isinstance(engine, str) or not engine:
                continue

            try:
                target = NativeTarget(
                    session_id=record["native_id"],
                    tag=record["tag"],
                    workspace=record["workspace"],
                    created_at_ms=incarnation,
                    engine=engine,
                ).validated()
            except (SessionControlError, KeyError, ValueError, TypeError):
                continue

            if time.time() - incarnation / 1000 < 600 or record.get('diagnostic', {}).get('reason') == 'engine_tool_permission_refused':
                health = self._check_headless_health(offer_id, record['task_id'], record['workspace'], engine)
                if health is not None:
                    reconciled.append(health)
                    continue

            # 2. Positive retirement verification
            try:
                is_retired, tombstone, detail = verify_native_retirement(native, target, state_dir=state_dir)
            except Exception:
                continue

            if not is_retired:
                continue

            # 3. Maintained Launcher reconcile
            if not has_launcher:
                continue

            prefix = launcher["argv"] + ["--config-dir", launcher["config_dir"]]
            task_id = record["task_id"]
            reconcile_argv = prefix + ["reconcile", "--id", task_id, "--json"]
            try:
                reconcile_out = self.runner(reconcile_argv, cwd=launcher["root"])
            except Exception:
                continue

            if not isinstance(reconcile_out, dict):
                continue

            reconciled_state = reconcile_out.get("state")
            resources_reserved = reconcile_out.get("resources_reserved")

            # Require confirmed physical release of the same native execution.
            if (resources_reserved is not False
                    or reconcile_out.get("native_id") != record["native_id"]
                    or reconciled_state not in ("completed-awaiting-review", "accepted", "failed")):
                continue

            # 4. Own-host Fleet terminal result
            fleet_state = "failed" if reconciled_state == "failed" or record.get('diagnostic', {}).get('reason') == 'engine_tool_permission_refused' else "exited"
            result_payload = {
                "state": fleet_state,
                "confirmed_exit": True,
                "native_id": record["native_id"],
                "launcher_task_id": task_id,
            }
            # Intake 88b3260d AC1: forward the worker's final result file so
            # the server can append it to the linked intake. Bounded,
            # symlink-confined, truncated; never logs tokens.
            try:
                forwarded = self._read_hub_finish_report(record)
                if forwarded:
                    result_payload.update(forwarded)
            except Exception:
                pass
            if record.get('diagnostic', {}).get('reason') == 'engine_tool_permission_refused':
                result_payload['diagnostic'] = record['diagnostic']
            if fleet_state == "failed":
                from .runtime_diagnostics import runtime_diagnostic
                runtime_failure = runtime_diagnostic(record, tombstone or {})
                if runtime_failure:
                    result_payload.setdefault("diagnostic", runtime_failure)
                fatal_runtime = runtime_failure and (runtime_failure['reason'] in {
                    'engine_oom_killed', 'engine_runtime_timeout', 'engine_signal_exit'}
                    or runtime_failure.get('exit_code') not in (None, 0))
                from .launch_diagnostics import check_engine_auth_expired
                raw_text = str(reconcile_out.get('reason') or reconcile_out.get('error') or '')
                if not fatal_runtime and check_engine_auth_expired(raw_text, engine=engine, cwd=record.get("workspace"), task_id=task_id, launched_at=record.get("launched_at_ts") or (record.get("incarnation", 0) / 1000.0 if record.get("incarnation") else None), start_offsets=record.get("log_offsets")):
                    result_payload["diagnostic"] = {
                        "reason": "engine_auth_expired",
                        "message": "The engine authentication failed or API key has expired.",
                        "error_type": "AuthenticationError",
                        "stage": "dispatch_headless",
                    }
            path = f"/api/fleet/launches/{offer_id}/result"
            try:
                acknowledgement = self.request("POST", path, result_payload)
                if (not isinstance(acknowledgement, dict)
                        or acknowledgement.get("id") != offer_id
                        or acknowledgement.get("state") != fleet_state):
                    continue
                record["settled"] = True
                record["state"] = fleet_state
                record["settled_at"] = utcnow()
                if result_payload.get("diagnostic"):
                    record["diagnostic"] = result_payload["diagnostic"]
                self._persist_launch_record(record)
                reconciled.append({"id": offer_id, "state": fleet_state, "reconciled": True})
                if record.get("workspace"):
                    self._maybe_cleanup_worktree(Path(record["workspace"]), record.get("native_id"))
            except Exception:
                # Retain pending observation until the server explicitly acknowledges it.
                continue

        return reconciled

    def _maybe_cleanup_worktree(self, workspace: Path, native_id: Optional[str] = None):
        """Attempt safe auto-cleanup of finished headless worktree if configured in cleanup_roots."""
        cleanup_roots = self.settings.get("cleanup_roots")
        if not cleanup_roots or not isinstance(cleanup_roots, list):
            return
        try:
            ws_str = str(workspace.resolve())
            from .session_cleanup import remove_owned_worktree
            from .session_executor import CallerCustody
            machine_id = self.settings.get("machine_id", "local")
            custody = CallerCustody(actor=machine_id, machine_id=machine_id, credential_family=f"host-{machine_id}")
            journal_dir = self.settings.get("journal_dir") or str(Path.home() / ".local" / "agent-hub" / "journal")
            Path(journal_dir).mkdir(parents=True, exist_ok=True)
            remove_owned_worktree(
                custody=custody,
                journal_dir=journal_dir,
                machine_id=machine_id,
                native_id=native_id or "headless-task",
                canonical_workspace=ws_str,
                cleanup_roots=cleanup_roots,
                claim_holder=machine_id,
                stop_confirmed=True,
            )
        except Exception:
            # Cleanup is best-effort and must not break the reporting flow
            pass

    def reconcile_imported_sessions(self, imported_bindings: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        """Reconcile active imported/observed-native sessions via verified positive retirement
        tombstones and own-host session retirement reporting.
        """
        reconciled = []
        if imported_bindings is None:
            cursor = self._get_imported_cursor()
            path = "/api/sessions/host-bindings?kind=imported"
            if cursor:
                import urllib.parse
                path += f"&cursor={urllib.parse.quote(cursor)}"
            try:
                res = self.request("GET", path)
                if not isinstance(res, dict) or "next_cursor" not in res:
                    return reconciled
                sessions = res.get("sessions")
                next_cursor = res.get("next_cursor")
                if (not isinstance(sessions, list) or len(sessions) > 100
                        or (next_cursor is not None and not self._valid_imported_cursor(next_cursor))):
                    return reconciled
                imported_bindings = sessions
                self._set_imported_cursor(next_cursor)
            except Exception:
                # Unknown/error preserves pending, invalidcursorstate fails safely without terminal inference
                return reconciled

        if not isinstance(imported_bindings, list) or not imported_bindings:
            return reconciled

        from .native_sessions import NativeTarget, SessionControlError

        native = self.settings.get("aplexer_bin", "aplexer")
        state_dir = self.settings.get("aplexer_state_dir")

        for item in imported_bindings:
            if not isinstance(item, dict):
                continue

            binding = item.get("binding")
            if not isinstance(binding, dict):
                continue

            if binding.get("provenance") != "observed_native":
                continue

            native_id = binding.get("native_id")
            if item.get("id") != native_id:
                continue
            tag = binding.get("tag")
            workspace = binding.get("workspace")
            incarnation = binding.get("native_created_at_ms")
            generation = binding.get("generation")
            engine = item.get("engine") or binding.get("engine")

            if not isinstance(native_id, str) or not native_id:
                continue
            if not isinstance(tag, str) or not tag:
                continue
            if not isinstance(workspace, str) or not workspace:
                continue
            if type(incarnation) is not int or incarnation <= 0:
                continue
            if type(generation) is not int or generation <= 0:
                continue
            if not isinstance(engine, str) or not engine:
                continue

            # 1. Corroborate exact 5-field native tuple
            try:
                target = NativeTarget(
                    session_id=native_id,
                    tag=tag,
                    workspace=workspace,
                    created_at_ms=incarnation,
                    engine=engine,
                ).validated()
            except (SessionControlError, ValueError, TypeError, KeyError):
                continue

            # 2. Positive retirement verification via authentic tombstone
            try:
                is_retired, tombstone, detail = verify_native_retirement(
                    native, target, state_dir=state_dir
                )
            except Exception:
                continue

            if not is_retired or not isinstance(tombstone, dict):
                continue

            cause = tombstone.get("cause")
            finished_at_ms = tombstone.get("finished_at_ms")
            if cause != "finished" or type(finished_at_ms) is not int or finished_at_ms <= 0:
                continue

            # 3. Report terminal positive retirement to Hub
            payload = {
                "native": {
                    "native_id": target.session_id,
                    "tag": target.tag,
                    "workspace": target.workspace,
                    "created_at_ms": target.created_at_ms,
                    "engine": target.engine,
                },
                "generation": generation,
                "finished_at_ms": finished_at_ms,
                "cause": "finished",
                "state": "stopped",
            }
            path = f"/api/sessions/{target.session_id}/retirement"
            try:
                ack = self.request("POST", path, payload)
                if isinstance(ack, dict):
                    ack_id = ack.get("id")
                    if ack_id == target.session_id and ack.get("state") == "stopped":
                        reconciled.append({
                            "id": target.session_id,
                            "state": "stopped",
                            "reconciled": True,
                        })
            except Exception:
                # Retain pending observation on transport/server failure;
                # reconcile next cycle.
                continue

        return reconciled

    def _derive_qualified_role_jobs(self) -> list[str]:
        """Derive only enabled real qualified supported role profiles via RuntimeManager.

        Rejects fixture/mock reports and arbitrary configured jobs text. Missing,
        disabled, or broken config or failed qualification returns empty list so any
        stale capability is removed on actual registration.
        """
        from .role_dispatch import load_role_config, SUPPORTED_ROLE_PROFILES, TrustedRoleConfig
        from .runtime import RuntimeManager

        cfg = None
        path = self.role_config_path or (self.settings.get("role_config") if isinstance(self.settings.get("role_config"), str) else None)
        if path:
            try:
                cfg = load_role_config(path)
            except Exception:
                cfg = None
        elif self.role_config is not None:
            if isinstance(self.role_config, TrustedRoleConfig):
                cfg = self.role_config
            elif isinstance(self.role_config, dict):
                try:
                    cfg = load_role_config(self.role_config)
                except Exception:
                    cfg = None
            elif isinstance(self.role_config, str):
                try:
                    cfg = load_role_config(self.role_config)
                except Exception:
                    cfg = None
        elif "role_config" in self.settings:
            rc_val = self.settings["role_config"]
            if isinstance(rc_val, TrustedRoleConfig):
                cfg = rc_val
            elif isinstance(rc_val, dict):
                try:
                    cfg = load_role_config(rc_val)
                except Exception:
                    cfg = None

        if cfg is None or not getattr(cfg, "enabled", False):
            return []

        profiles = getattr(cfg, "profiles", {})
        if not isinstance(profiles, dict) or not profiles:
            return []

        qualified = []
        try:
            mgr = RuntimeManager(role_config=cfg)
        except Exception:
            return []

        for kind in sorted(SUPPORTED_ROLE_PROFILES):
            p_cfg = profiles.get(kind)
            if not p_cfg or not getattr(p_cfg, "enabled", False):
                continue
            try:
                ok, reason, report = mgr.can_process_role_run(kind)
            except Exception:
                continue

            if not ok:
                continue
            # Hard rejection of fixture/mock reports
            if reason != "role_qualified" or "mock" in str(reason).lower() or "fixture" in str(reason).lower():
                continue
            if not isinstance(report, dict):
                continue
            if report.get("is_fixture_only") is True or report.get("status") == "MOCK_FIXTURE":
                continue
            if report.get("operational_qualified") is False:
                continue
            if report.get("supported") is not True or report.get("config_enabled") is not True:
                continue

            qualified.append(kind)

        return sorted(qualified)

    def goal_routes(self):
        """Advertise executable local routes, independent of quota admission."""
        return [{'engine': sub.get('engine', sub['service']), 'account': sub['account_label'],
                 'profile': sub.get('profile')}
                for sub in self.settings['subscriptions']
                if sub.get('engine', sub['service']) in {'codex', 'zcodex'}
                and sub.get('admission_argv')
                and (sub['account_label'] in self._managed_labels or
                     (isinstance(sub.get('startup_argv'), list) and sub['startup_argv']
                      and all(isinstance(arg, str) and arg and '\0' not in arg for arg in sub['startup_argv'])))]

    def _route(self, offer):
        matches = [s for s in self.settings["subscriptions"] if s["account_label"] == offer.get("account") and s.get("engine", s["service"]) == offer.get("engine") and s.get("profile") == offer.get("profile")]
        if len(matches) != 1:
            raise ValueError("Local account/profile/workspace is not configured")
        # Validates map membership or workspace_roots / worktrees identity paths.
        resolve_workspace_path(self.settings, offer.get("workspace"))
        sub = dict(matches[0])
        if offer.get('mode') == 'headless' and sub.get('launcher_provider') == 'claude' and not sub.get('profile'):
            raise ValueError('model_unsupported: Claude headless route requires managed profile')
        if offer.get("selection") is not None:
            from .model_contract import configured_selection
            model = configured_selection(offer["selection"], offer["engine"], sub.get("launcher_provider") or sub["service"])
            sub["launcher_model"] = model["model"]
            sub["launcher_effort"] = model["effort"]
        if not sub.get("admission_argv"):
            raise ValueError("admission_unconfigured: maintained fresh admission adapter is not configured")
        return sub

    def _verify_native_cwd(self, native, native_id, target_cwd, receipt):
        """Confirm the started session's actual catalog cwd (receipt then list).

        The start receipt carries the effective cwd when the native CLI
        reports it; otherwise the live catalog is scanned for the new native
        id. Either must equal the resolved offer cwd — the hub never claims an
        executed cwd it cannot see.
        """
        if not isinstance(native_id, str) or not native_id:
            return False
        receipt_cwd = receipt.get("cwd") if isinstance(receipt, dict) else None
        if isinstance(receipt_cwd, str) and receipt_cwd:
            return receipt_cwd == str(target_cwd)
        try:
            rows = self.runner([native, "list", "--json"])
        except Exception:
            return False
        rows = rows if isinstance(rows, list) else (rows.get("sessions") if isinstance(rows, dict) else None)
        if not isinstance(rows, list):
            return False
        for row in rows:
            if isinstance(row, dict) and str(row.get("id", "")).lower() == native_id.lower():
                return row.get("cwd") == str(target_cwd)
        return False

    def _drop_queued_launcher_task(self, launcher, task_id):
        """Best-effort clear of a still-queued launcher row for offer retry.

        Hub may re-dispatch the same offer id after a prior partial attempt
        whose payload drifted (e.g. account_label omit). Launcher submit then
        raises Conflicting payload; dropping only `queued` rows lets dispatch
        resubmit without touching running work.
        """
        import sqlite3
        config_dir = launcher.get("config_dir")
        if not isinstance(config_dir, str) or not config_dir:
            return
        db = Path(config_dir) / "state.db"
        if not db.is_file() or db.is_symlink():
            return
        try:
            with sqlite3.connect(str(db), timeout=5) as conn:
                row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if not row or row[0] != "queued":
                    return
                conn.execute("DELETE FROM task_paths WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM task_resources WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM tasks WHERE id = ? AND state = 'queued'", (task_id,))
                conn.commit()
        except Exception:
            return

    def _submit_launcher_task(self, prefix, launcher, task_id, submit_argv):
        try:
            return self.runner(submit_argv, cwd=launcher["root"])
        except RuntimeError:
            self._drop_queued_launcher_task(launcher, task_id)
            return self.runner(submit_argv, cwd=launcher["root"])

    def dispatch(self, offer):
        if offer.get('with_goal') and offer.get('mode') != 'interactive':
            raise ValueError('Goal work is interactive only')
        sub = self._route(offer)
        device_auth = self.settings.get('opencode_auth') if sub.get('launcher_provider') == 'opencode' else None
        if device_auth:
            from .opencode_auth import prepare
            prepare(device_auth)  # Before quota timestamp, task submission and claim.
        selection = offer.get('selection')
        if offer.get('task_id'):
            if not isinstance(offer.get('needs'), list) or not set(offer['needs']).issubset(self.settings.get('capabilities', [])):
                raise ValueError('Required capabilities are no longer available locally')
            if not isinstance(selection, dict) or selection.get('model') != sub.get('launcher_model') or selection.get('effort') != sub.get('launcher_effort'):
                raise ValueError('Selected model/effort is not configured locally')
        offer_id = offer.get("id")
        if not isinstance(offer_id, str) or not LABEL.fullmatch(offer_id):
            raise ValueError("Invalid offer id")
        if offer.get("task_id") is not None:
            if not isinstance(offer["task_id"], str) or not LABEL.fullmatch(offer["task_id"]):
                raise ValueError("Invalid task id")
        gate = self.runner(sub["admission_argv"], payload={k: offer.get(k) for k in ["mode", "engine", "profile", "account", "workspace", "selection"]})
        if gate.get("launch_allowed") is not True or gate.get("profile_supported") is not True or gate.get("account_label") != sub["account_label"]:
            gate_reason = gate.get("reason") if isinstance(gate.get("reason"), str) else ""
            # Preserve host-side gate slugs (intake 59a6f44a AC3/AC4): a managed
            # Codex quota-gate refusal must surface as codex_quota_gate_* in
            # rejected[]/launch_error, never a silent stopped/session_not_bound.
            if gate_reason.startswith("codex_quota_gate_"):
                slug = ''.join(c if (c.isalnum() or c == '_') else '_' for c in gate_reason)[:64]
                raise ValueError(slug + ": managed Codex quota gate refused launch before execution")
            raise ValueError("Fresh admission refused")
        if (offer.get('task_id') or (selection and sub.get("account_label") in self._managed_labels)) and (gate.get('model_supported') is not True or gate.get('model') != selection['model'] or gate.get('effort') != selection['effort']):
            raise ValueError('Selected model/effort requires verified admission')
        at = gate.get("observed_at")
        try:
            observed = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds()
            if not 0 <= age <= 30:
                raise ValueError()
        except Exception:
            raise ValueError("Fresh admission timestamp required") from None
        mode = offer.get("mode")
        # The offer workspace is a configured identifier — a legacy label or
        # an explicitly allowed absolute path — never a free-form path: it is
        # mapped through the operator-configured workspaces exactly as _route
        # validated above. A literal offer string must never become a
        # filesystem path on its own (label "demo" is not ./demo, and an
        # unconfigured "/abs/path" is refused before any claim or effect).
        workspace = resolve_workspace_path(self.settings, offer["workspace"])
        target_cwd = resolve_offer_cwd(workspace, offer.get("cwd"))
        native = self.settings.get("aplexer_bin", "aplexer")
        if mode == "interactive":
            if selection:
                launcher = self.settings.get('launcher', {})
                if not all(launcher.get(k) for k in ('argv', 'root', 'config_dir')):
                    raise ValueError('model_unsupported: maintained adapter capability query unavailable')
                try:
                    support = self.runner(launcher['argv'] + ['--config-dir', launcher['config_dir'], 'model-support',
                                          '--provider', sub['launcher_provider'], '--model', selection['model']], cwd=launcher['root'])
                except Exception:
                    raise ValueError('model_unsupported: maintained adapter capability query unavailable') from None
                if support.get('supported') is not True or support.get('provider') != sub['launcher_provider'] or support.get('model') != selection['model']:
                    raise ValueError('model_unsupported: maintained adapter refused requested model')
            with_goal = offer.get('with_goal') is True
            if with_goal and (offer.get('engine') not in {'codex', 'zcodex'} or not isinstance(offer.get('prompt'), str) or not offer['prompt'].strip()):
                raise ValueError('Interactive goal requires a Codex-family engine and goal text')
            if offer.get("prompt") and not with_goal:
                raise ValueError("Interactive task injection is unsupported; start an empty native session")
            argv = [native, "start", "--workspace", str(workspace), "--tag", "hub-" + offer_id, "--engine", offer["engine"], "--memory", WORKER_MEMORY, "--pids", WORKER_PIDS, "--cpu-quota-us", WORKER_CPU_QUOTA_US, "--cpu-period-us", WORKER_CPU_PERIOD_US, "--json"]
            if offer.get("profile"):
                argv += ["--profile", offer["profile"]]
            if target_cwd != workspace:
                argv += ["--cwd", str(target_cwd)]
            git_ident = normalize_git_identity(self.settings.get("git") if "git" in self.settings else self.settings.get("git_identity"))
            if not git_ident:
                from . import provisioning
                git_ident = provisioning.load_git_identity()
            git_name = git_ident.get("user.name") or git_ident.get("name")
            git_email = git_ident.get("user.email") or git_ident.get("email")
            if git_name:
                argv += ["--env", f"GIT_AUTHOR_NAME={git_name}", "--env", f"GIT_COMMITTER_NAME={git_name}"]
            if git_email:
                argv += ["--env", f"GIT_AUTHOR_EMAIL={git_email}", "--env", f"GIT_COMMITTER_EMAIL={git_email}"]
            if selection and not sub.get("startup_argv") and sub.get("account_label") not in self._managed_labels:
                argv += ["--model", selection["model"]]
            if with_goal or sub.get("startup_argv") or (selection and sub.get("account_label") in self._managed_labels):
                launch = sub.get('startup_argv')
                if (with_goal or selection) and sub.get('account_label') in self._managed_labels:
                    from . import provisioning
                    launch = provisioning.module_command('execute', sub['account_label'])
                elif with_goal and not launch:
                    raise ValueError('Goal startup requires a trusted local command')
                if not isinstance(launch, list) or not launch or any(not isinstance(a, str) or "\0" in a for a in launch):
                    raise ValueError("Invalid trusted startup argv")
                if selection:
                    from .model_contract import replace_model_args
                    launch = replace_model_args(launch, selection["model"])
                argv += ["--"] + launch + (['/goal ' + compose_prompt(offer['prompt']) + '\nContainer hygiene: use docker run --rm for throwaway/test containers; label every test container agent.session=<your Hub session id, or native session id registered in Hub>. Install trap/finally cleanup before creation and clean up all resources you create before session exit, including failed starts and docker create/compose tests. Preserve other sessions and Coolify containers.'] if with_goal else [])
            prefix = sub.get("native_start_argv") or self.settings.get("native_start_argv")
            if prefix is not None:
                prefix = validate_native_start_argv(prefix)
                argv = list(prefix) + argv
            cwd = None
        elif mode == "headless":
            profile = sub.get("profile")
            if profile is not None:
                if not isinstance(profile, str) or not LABEL.fullmatch(profile):
                    raise ValueError("Invalid provider profile")
                if not (profile == f"hub-{sub.get('account_label')}" or (hasattr(self, "_managed_labels") and sub.get("account_label") in self._managed_labels)):
                    raise ValueError("Headless provider profile is not qualified")
            launcher = self.settings.get("launcher", {})
            if not all(launcher.get(k) for k in ["argv", "root", "config_dir"]) or not sub.get("launcher_provider") or not sub.get("launcher_model"):
                raise ValueError("Maintained launcher route is not configured")
            if sub.get("launcher_provider") not in {"opencode", "antigravity", "zai", "codex", "gemini", "claude"}:
                raise ValueError(f"Launcher provider {sub.get('launcher_provider')!r} is not supported for headless execution")
            # Only the maintained launcher may run models. argv is a trusted machine-local CLI prefix.
            prefix = launcher["argv"] + ["--config-dir", launcher["config_dir"]]
            if device_auth:
                from .opencode_auth import launch_argv
                prefix = launch_argv(device_auth, prefix)
            task_id = "hub-" + offer_id
            goal = compose_prompt(offer["prompt"]) + "\nContainer hygiene: use docker run --rm for throwaway/test containers; label every test container agent.session=<your Hub session id, or native session id registered in Hub>. Install trap/finally cleanup before creation and clean up all resources you create before session exit, including failed starts and docker create/compose tests. Preserve other sessions and Coolify containers."
            curr = workspace
            for seg in (".local", "hub-results"):
                curr = curr / seg
                if curr.is_symlink():
                    raise ValueError(f"Refuse symlinked ancestor directory: {curr}")
                if not curr.exists():
                    curr.mkdir(mode=0o700, exist_ok=True)
                    os.chmod(curr, 0o700)
                st = os.lstat(curr)
                if not stat.S_ISDIR(st.st_mode) or curr.is_symlink():
                    raise ValueError(f"Ancestor directory is not a regular directory: {curr}")
            os.chmod(curr, 0o700)
            result_path = curr / (offer_id + ".json")
            if result_path.is_symlink():
                raise ValueError(f"Refuse symlinked result leaf: {result_path}")
            try:
                result_path.relative_to(workspace)
            except ValueError:
                raise ValueError("Result path escapes workspace root")
            goal += '\nWrite a final JSON object to ' + str(result_path) + ': {"status":"done|failed|waiting-on-human|stuck","report":"sanitized final report","artifacts":[{"label":"text","text":"sanitized text"}]}. No secrets, codes, tokens or URLs. Do not write raw screenshots; keep them private for human review locally.'
            # Launcher named-profile admission requires BOTH account_label and
            # account_profile. Unprofiled native engines (antigravity/opencode)
            # must omit account_label; a bare account_label makes every provider
            # reject with "Named profile requires both account_label and
            # account_profile", leaving Hub launches stuck in offered forever.
            payload = {"goal": goal, "owner": "hub-fleet", "cwd": str(target_cwd), "timeout": 1800,
                       "model_requirements": {"providers": [sub["launcher_provider"]], "models": [sub["launcher_model"]]},
                       "profile": "model-task"}
            if profile is not None:
                payload["account_label"] = sub["account_label"]
                payload["account_profile"] = profile
                payload["provider_profile"] = profile
                from . import provisioning
                payload["managed_dir"] = str(provisioning.root())
            submit_argv = prefix + ["submit", "--id", task_id, "--key", task_id, "--paths", str(result_path), "--payload", json.dumps(payload), "--json"]
            self._submit_launcher_task(prefix, launcher, task_id, submit_argv)
            plan = self.runner(prefix + ["plan", "--id", task_id], cwd=launcher["root"])
            eligible = plan.get("eligible", [])
            # Real maintained-plan contract (verified live against the
            # maintained CLI dry-run): entries carry provider/model/task_fit
            # plus ranking metadata, never Hub account_label/profile. Account
            # and profile stay bound by the exact local subscription match in
            # _route plus the fresh admission-gate account_label below; the
            # plan proves the maintained launcher would actually select this
            # provider/model (known task_fit 1.0, never fabricated). Entries
            # that do carry account_label/profile must match when present.
            def _plan_match(r):
                if not isinstance(r, dict):
                    return False
                if r.get("provider") != sub["launcher_provider"] or r.get("model") != sub["launcher_model"]:
                    return False
                if r.get("task_fit") != 1.0:
                    return False
                if "account_label" in r and r.get("account_label") != sub["account_label"]:
                    return False
                if "profile" in r and r.get("profile") != sub.get("profile"):
                    return False
                return True
            if not isinstance(eligible, list) or not any(_plan_match(r) for r in eligible):
                raise ValueError("model_unsupported: maintained launcher plan refused requested model")
            # OpenCode, Antigravity, ZAI, Gemini, and named-profile Codex use the maintained native backend:
            # real engine/UUID plus validated first action for Hub binding.
            # Other unprofiled providers retain their existing task-unit semantics.
            if sub.get("launcher_provider") in {"opencode", "antigravity", "zai", "gemini", "claude"} or (sub.get("launcher_provider") == "codex" and sub.get("profile") is not None):
                argv = prefix + ["run", "--backend", "aplexer",
                                 "--id", task_id, "--cwd", str(workspace),
                                 "--tmpdir", str(workspace / ".local" / "tmp")]
            else:
                argv = prefix + ["run", "--backend", "task-units", "--as-controller", "--id", task_id, "--cwd", str(target_cwd), "--tmpdir", str(workspace / ".local" / "tmp")]
            cwd = launcher["root"]
        else:
            raise ValueError("Unsupported launch mode")
        if device_auth and mode == 'interactive':
            from .opencode_auth import launch_argv
            argv = launch_argv(device_auth, argv)
        path = "/api/fleet/launches/" + offer_id
        # An uncertain response here leaves the server reservation intact and performs no native effect.
        self.request("POST", path + "/claim", {"gate_allowed": True, "profile_supported": True, "observed_at": at, "account": sub["account_label"], **({"model": selection["model"]} if selection else {})})
        from .launch_diagnostics import capture_log_offsets
        log_offsets = capture_log_offsets(sub.get("launcher_provider"))
        launched_at_ts = time.time()
        try:
            if mode == "interactive" and not offer.get("with_goal"):
                # Empty interactive starts must remain empty: queue the trusted
                # preamble in the native inbox before the engine's awareness hook
                # runs. Never type into a PTY, overwrite a draft, or start a goal.
                # Same-workspace queued send only (cwd, no --workspace): a
                # --workspace send to a not-yet-existing tag is rejected as a
                # cross-workspace queued target before native binding exists.
                # Mirrors the host bridge's durable queued-send shape.
                self.runner([native, "message", "send",
                             "--to", "hub-" + offer_id, "--queue",
                             "--kind", "hub-session-preamble", "--json", SESSION_PREAMBLE],
                            cwd=str(workspace))
            receipt = self.runner(argv, cwd=cwd, timeout=120)
            if mode == "interactive":
                native_id = receipt.get("id")
                if selection:
                    from .model_contract import native_model
                    if native_model(receipt.get("command")) != selection["model"]:
                        raise ValueError("model_mismatch: native interactive model is unverified")
                if receipt.get("workspace") != str(workspace) or receipt.get("tag") != "hub-" + offer_id or receipt.get('engine') != offer['engine'] or receipt.get('profile') != offer.get('profile') or receipt.get('phase') != 'running':
                    raise ValueError("Native receipt mismatch")
                if target_cwd != workspace and not self._verify_native_cwd(native, native_id, target_cwd, receipt):
                    raise ValueError("Native cwd mismatch")
                result = {"state": "running", "native_id": native_id}
            else:
                details = receipt.get("receipt", {})
                if selection and (details.get("model") != selection["model"] or details.get("model_verified") is not True):
                    raise ValueError("model_mismatch: native launcher model is unverified")
                if sub.get("launcher_provider") in {"opencode", "antigravity", "zai", "gemini", "claude"} or (sub.get("launcher_provider") == "codex" and sub.get("profile") is not None):
                    # Opencode, Antigravity, ZAI, Gemini, and named-profile Codex bind real M12 sessions only:
                    # a genuine native UUID is required; task-units unit_name/worker_id names
                    # can never bind or control a session.
                    session_id = details.get("session_id")
                    native_id = session_id if isinstance(session_id, str) and session_id else None
                else:
                    native_id = details.get("session_id") or details.get("unit_name") or details.get("worker_id")
                if sub.get("launcher_provider") == "zai":
                    # ZAI session control requires a real native ZCodex
                    # identity, never a task-unit identifier or SHELL label.
                    try:
                        if str(uuid.UUID(native_id)) != native_id.lower() or uuid.UUID(native_id).version != 4:
                            raise ValueError("Noncanonical native UUID")
                    except (ValueError, TypeError, AttributeError):
                        raise ValueError("Native ZCodex UUID required") from None
                    expected_zai_engine = "codex" if sub.get("profile") is not None else "zcodex"
                    if (details.get("engine") != expected_zai_engine or offer.get("engine") != expected_zai_engine
                            or details.get("workspace") != str(workspace)
                            or details.get("tag") != "task-" + task_id):
                        raise ValueError("Native ZCodex receipt mismatch")
                if sub.get("launcher_provider") == "gemini":
                    try:
                        if str(uuid.UUID(native_id)) != native_id.lower() or uuid.UUID(native_id).version != 4:
                            raise ValueError("Noncanonical native UUID")
                    except (ValueError, TypeError, AttributeError):
                        raise ValueError("Native Gemini UUID required") from None
                    if (details.get("engine") != "gemini" or offer.get("engine") != "gemini"
                            or details.get("workspace") != str(workspace)
                            or details.get("tag") != "task-" + task_id):
                        raise ValueError("Native Gemini receipt mismatch")
                if sub.get("launcher_provider") == "codex" and sub.get("profile") is not None:
                    try:
                        if str(uuid.UUID(native_id)) != native_id.lower() or uuid.UUID(native_id).version != 4:
                            raise ValueError("Noncanonical native UUID")
                    except (ValueError, TypeError, AttributeError):
                        raise ValueError("Native Codex UUID required") from None
                    if (details.get("engine") != "codex" or offer.get("engine") != "codex"
                            or details.get("workspace") != str(workspace)
                            or details.get("tag") != "task-" + task_id):
                        raise ValueError("Native Codex receipt mismatch")
                if sub.get("profile") is not None:
                    rec_profile = details.get("profile")
                    if not rec_profile or rec_profile != sub["profile"]:
                        raise ValueError("Native profile receipt mismatch")
                if "account_label" in details:
                    if details.get("account_label") != sub["account_label"]:
                        raise ValueError("Native account receipt mismatch")
                elif sub.get("profile") is not None:
                    raise ValueError("Native account receipt mismatch")
                if type(details.get('exit_code')) is int:
                    result = {'state': 'exited' if details['exit_code'] == 0 else 'failed', 'confirmed_exit': True, 'native_id': native_id, 'launcher_task_id': task_id}
                    if details['exit_code'] != 0:
                        from .launch_diagnostics import check_engine_auth_expired
                        engine_name = offer.get("engine") or (sub.get("engine") if 'sub' in locals() and isinstance(sub, dict) else None) or (sub.get("service") if 'sub' in locals() and isinstance(sub, dict) else None)
                        raw_text = str(details.get('stderr') or details.get('stdout') or details.get('error') or '')
                        if check_engine_auth_expired(raw_text, engine=engine_name, cwd=target_cwd, task_id=task_id, launched_at=launched_at_ts, start_offsets=log_offsets):
                            result['diagnostic'] = {
                                'reason': 'engine_auth_expired',
                                'message': 'The engine authentication failed or API key has expired.',
                                'error_type': 'AuthenticationError',
                                'exit_code': details['exit_code'],
                                'stage': 'launcher_run',
                            }
                elif details.get('state') == 'running' and details.get('first_action_verified') is True:
                    result = {"state": "running", "native_id": native_id, "launcher_task_id": task_id}
                else:
                    raise ValueError('Native lifecycle receipt is incomplete')
            if not isinstance(native_id, str) or not native_id:
                raise ValueError("No confirmed native execution receipt")
            if mode == "headless" and not offer.get("task_id") and result.get("state") == "running":
                self._record_running_launch(
                    offer_id=offer_id,
                    task_id=task_id,
                    workspace=workspace,
                    engine=offer.get("engine") or sub.get("engine") or sub.get("service"),
                    account=sub.get("account_label"),
                    native_id=native_id,
                    result_path=result_path,
                    details=details,
                    profile=offer.get("profile") or sub.get("profile"),
                    launched_at_ts=launched_at_ts,
                    log_offsets=log_offsets,
                )
                health = self._check_headless_health(offer_id, task_id, workspace, offer.get('engine') or sub.get('engine'))
                if health is not None:
                    return health
        except Exception as exc:
            from .launch_diagnostics import exception_diagnostic, LocalCommandError
            engine_name = offer.get("engine") or (sub.get("engine") if 'sub' in locals() and isinstance(sub, dict) else None) or (sub.get("service") if 'sub' in locals() and isinstance(sub, dict) else None)
            t_id = locals().get("task_id")
            t_cwd = locals().get("target_cwd") or locals().get("workspace")
            l_offsets = locals().get("log_offsets")
            l_at = locals().get("launched_at_ts")
            failure = exception_diagnostic(exc, "dispatch_headless" if mode == "headless" else "dispatch_interactive",
                                           engine=engine_name, cwd=t_cwd, task_id=t_id, launched_at=l_at, start_offsets=l_offsets)
            if mode == "headless" and not offer.get("task_id") and t_id:
                try:
                    health = self._check_headless_health(offer_id, t_id, workspace, engine_name)
                    if health is not None:
                        return health
                except Exception:
                    pass  # Original failure remains durable; no invented exit proof.
                from .launcher_recovery import settle_unstarted
                record = {"offer_id": offer_id, "task_id": task_id, "mode": "headless",
                          "workspace": str(workspace), "target_cwd": str(target_cwd),
                          "state": "not_started", "reason": failure["reason"], "diagnostic": failure, "settled": False}
                try:
                    self._persist_launch_record(record)
                    self._active_launches[offer_id] = record
                    proven_dead = settle_unstarted(launcher, task_id, workspace, target_cwd, failure)
                    native_session_id = None
                    if not proven_dead:
                        start_path = Path(workspace) / ".local" / f"launch-{task_id}-start.json"
                        if start_path.exists():
                            try:
                                from .native_sessions import NativeTarget
                                from .retired_native import verify_native_retirement
                                s_data = json.loads(start_path.read_text())
                                cand_id = s_data.get("native_id") or s_data.get("session_id") or s_data.get("id")
                                cand_tag = s_data.get("tag") or f"task-{task_id}"
                                cand_ws = s_data.get("workspace") or str(workspace)
                                cand_engine = s_data.get("engine") or offer.get("engine")
                                cand_inc = s_data.get("created_at_ms") or s_data.get("incarnation")
                                if cand_id and cand_inc:
                                    target = NativeTarget(
                                        session_id=cand_id,
                                        tag=cand_tag,
                                        workspace=str(Path(cand_ws).resolve(strict=True)),
                                        created_at_ms=cand_inc,
                                        engine=cand_engine,
                                    ).validated()
                                    is_retired, tomb, _ = verify_native_retirement(native, target, state_dir=self.settings.get("aplexer_state_dir"))
                                    if is_retired:
                                        proven_dead = True
                                        native_session_id = cand_id
                            except Exception:
                                pass
                        elif not (Path(launcher.get("config_dir", "")) / "state.db").exists() and isinstance(exc, LocalCommandError):
                            proven_dead = True

                    if proven_dead:
                        if native_session_id:
                            payload = {
                                "state": "failed",
                                "launcher_task_id": task_id,
                                "native_id": native_session_id,
                                "confirmed_exit": True,
                                "diagnostic": failure,
                            }
                        else:
                            payload = {
                                "state": "failed",
                                "launcher_task_id": task_id,
                                "confirmed_exit": True,
                                "reason": failure["reason"],
                                "diagnostic": failure,
                            }
                        ack = self.request("POST", path + "/result", payload)
                        if isinstance(ack, dict) and ack.get("id") == offer_id and ack.get("state") == "failed":
                            record["settled"] = True
                            self._persist_launch_record(record)
                        return {"id": offer_id, "state": "failed", "reason": failure["reason"], "diagnostic": failure}
                except Exception:
                    pass
            # Effect may have happened: hold capacity and require reconciliation.
            # Bounded private diagnostic only (type + stage, never argv/goal/
            # tokens/stdout). Returned locally and reported as uncertain reason
            # so real runtime start failures stay visible; server authority
            # unchanged (still uncertain, never claimed running).
            err = (type(exc).__name__ or "Error")[:64]
            stage = ("dispatch_headless" if mode == "headless" else "dispatch_interactive")[:32]
            reason = ("%s:%s" % (stage, err))[:120]
            try:
                self.request("POST", path + "/result", {"state": "uncertain", "diagnostic": failure})
            except Exception:
                pass
            return {"id": offer_id, "state": "uncertain", "reason": reason,
                    "stage": stage, "error": err, "diagnostic": failure}
        if offer.get('task_id') and result.get('confirmed_exit'):
            from .routing import read_text
            try:
                curr = workspace
                for seg in ('.local', 'hub-results'):
                    curr = curr / seg
                    if curr.is_symlink():
                        raise ValueError('Refuse linked ancestor directory')
                if result_path.is_symlink():
                    raise ValueError('Refuse linked result file')
                try:
                    result_path.relative_to(workspace)
                except ValueError:
                    raise ValueError('Result path escapes workspace')
                report = json.loads(read_text(result_path))
                if not isinstance(report, dict) or report.get('status') not in {'done', 'failed', 'waiting-on-human', 'stuck'}:
                    raise ValueError('Invalid final report')
                report = {'status': report['status'], 'report': report['report'], 'artifacts': report.get('artifacts', [])}
                if result['state'] == 'failed': report['status'] = 'failed'
            except Exception:
                report = {'status': 'failed', 'report': 'Agent exited without a valid structured report; inspect private local logs.', 'artifacts': []}
            report['confirmed_exit'] = True
            self.request('POST', '/api/fleet/tasks/' + offer['task_id'] + '/result', report)
            if report['status'] in {'done', 'failed'}:
                self._maybe_cleanup_worktree(workspace, native_id)
                return {'id': offer_id, 'state': report['status']}
            # Human waits retain reservations even when the reporting process exits.
            return {'id': offer_id, 'state': report['status']}
        self.request("POST", path + "/result", result)
        if result.get('state') in {'exited', 'failed'}:
            self._maybe_cleanup_worktree(workspace, native_id)
        return {"id": offer_id, **result}

    def report_catalog_bindings(self):
        """Report authoritative native identity, then bind matching Hub starts.

        Repeat immediately after each confirmed start so later offers and
        the next poll interval cannot delay its binding or double-count its
        native process alongside its reservation. Failure is safe to retry:
        this helper never launches anything.
        """
        raw = self.runner([self.settings.get("aplexer_bin", "aplexer"), "list", "--all", "--json"])
        from .session_progress import SessionProgress, stop_orphan
        if not hasattr(self, '_session_progress'):
            from .native_progress import NativeTranscriptProgress
            self._session_progress = SessionProgress(stop=lambda row: stop_orphan(self.settings.get('aplexer_bin', 'aplexer'), row),
                output=NativeTranscriptProgress(opencode_data=self.settings.get('opencode_auth', {}).get('target_data')),
                state_path=Path(self.journal_dir) / 'session-progress.json' if self.journal_dir else None)
        rows = raw if isinstance(raw, list) else raw.get('sessions', [])
        # Preserve native exit metadata before automatic retirement removes the row.
        for record in self._active_launches.values():
            for row in rows:
                if (row.get('id') == record.get('native_id')
                        and row.get('created_at_ms') == record.get('incarnation')
                        and row.get('tag') == record.get('tag')
                        and row.get('workspace') == record.get('workspace')
                        and row.get('engine') == record.get('engine')
                        and isinstance(row.get('exit'), dict)):
                    exit_info = row['exit']
                    safe = {k: exit_info[k] for k in ('code', 'signal', 'exited_at_ms')
                            if type(exit_info.get(k)) is int}
                    safe['oom_killed'] = exit_info.get('oom_killed') is True
                    if record.get('runtime_exit') != safe:
                        record['runtime_exit'] = safe
                        self._persist_launch_record(record)
        progress = self._session_progress.sample(rows, self._active_launches)
        self.request("PUT", "/api/fleet/sessions", {"sessions": session_metadata(raw, self.settings, progress)})
        # Reconcile Fleet-owned starts into exact Hub session bindings after
        # reporting the observed native catalog. Retry is read-before-write.
        try:
            pending = self.request("GET", "/api/sessions/host-bindings").get("sessions", [])
        except ValueError as exc:
            # Cloudflare may briefly route to the previous DO version during
            # rollout. Continue Fleet work; reconcile bindings next cycle.
            if "404" not in str(exc):
                raise
            pending = []
        rows = raw if isinstance(raw, list) else raw.get("sessions", [])
        for session in pending:
            if not isinstance(session, dict):
                continue
            row = find_binding_row(rows, session)
            if row is None:
                continue
            try:
                self.request("POST", "/api/sessions/" + session["id"] + "/binding", {"native": {"native_id": row["id"], "tag": row["tag"], "workspace": row["workspace"], "native_created_at_ms": row["created_at_ms"]}})
            except ValueError:
                # A refused binding (e.g. server fleet_tag_mismatch for a
                # launcher-backend tag before the server seam lands) must not
                # kill the cycle; the entry stays pending for next cycle.
                continue
        return raw

    def once(self):
        from . import provisioning
        provisioning.tick()
        managed = provisioning.managed_subscriptions()
        self.settings['subscriptions'] = [s for s in self.settings['subscriptions'] if s['account_label'] not in self._managed_labels] + managed
        self._managed_labels = {s['account_label'] for s in managed}
        from . import api, system
        info = system.get_system_info()
        caps = info['capabilities']
        caps.update(engines=sorted({s.get('engine', s['service']) for s in self.settings['subscriptions']}),
                    workspaces=advertised_workspaces(self.settings), cpu_count=os.cpu_count())
        caps['fleet_modes'] = ['interactive'] + (['headless'] if self.settings.get('launcher') else [])
        caps['launcher_resources'] = bool(self.settings.get('launcher'))
        caps['fleet_goal_routes'] = self.goal_routes()
        # The maintained launcher admits Claude headless work only through a
        # managed profile. Its global model-support list alone does not prove
        # this local account route is executable (interactive is independent).
        caps['fleet_route_rejections'] = [
            {'engine': s.get('engine', s['service']), 'account': s['account_label'],
             'profile': s.get('profile'), 'mode': 'headless', 'reason': 'model_unsupported'}
            for s in self.settings['subscriptions']
            if s.get('launcher_provider') == 'claude' and not s.get('profile')]
        qualified_jobs = self._derive_qualified_role_jobs()
        if qualified_jobs:
            caps['jobs'] = qualified_jobs
        else:
            caps.pop('jobs', None)
        self.request('POST', '/api/machines/register', info)
        hb = api.get_heartbeat_payload()
        if self.settings.get('launcher'):
            from .launcher_recovery import reconcile_resources
            resources = reconcile_resources(self.settings['launcher'], self.runner)
            if resources is not None:
                hb['launcher_resources'] = resources
        allowed = False
        admitted_engines = set()
        # This is only a scheduling projection. dispatch() repeats the actual mode/account gate.
        for sub in self.settings['subscriptions']:
            if not sub.get('admission_argv'):
                continue
            try:
                gate = self.runner(sub['admission_argv'], payload={'mode': 'interactive', 'engine': sub.get('engine', sub['service']), 'profile': sub.get('profile'), 'account': sub['account_label'], 'workspace': next(iter(self.settings.get('workspaces', {})), None)})
                at = dt.datetime.fromisoformat(gate['observed_at'].replace('Z', '+00:00'))
                age = (dt.datetime.now(dt.timezone.utc) - at).total_seconds()
                admitted = gate.get('launch_allowed') is True and gate.get('profile_supported') is True and gate.get('account_label') == sub['account_label'] and 0 <= age <= 30
                allowed |= admitted
                if admitted:
                    admitted_engines.add(sub.get('engine', sub['service']))
            except Exception:
                pass
        hb['gate'] = {'launch_allowed': allowed, 'reason': 'fleet local admission' if allowed else 'fleet admission unavailable', 'at': utcnow()}
        self.request('POST', '/api/host/poll', hb)
        for engine in sorted(admitted_engines):
            try:
                self.request('POST', '/api/fleet/probe', {'engine': engine, 'status': 'ok', 'reason': 'admission_configured'})
            except Exception:
                pass  # A failed projection never replaces dispatch admission.
        if self.settings.get('opencode_auth'):
            from .opencode_auth import prepare, AuthBusy
            try:
                prepare(self.settings['opencode_auth'])
                auth_health = {'engine': 'opencode', 'status': 'ok', 'reason': 'device_auth_verified'}
            except AuthBusy:
                auth_health = None
            except ValueError:
                auth_health = {'engine': 'opencode', 'status': 'unavailable', 'reason': 'engine_auth_expired'}
            try:
                if auth_health:
                    self.request('POST', '/api/fleet/probe', auth_health)
            except Exception:
                pass  # dispatch repeats the check before allowing any claim.
        self.request("PUT", "/api/fleet/subscriptions", {"subscriptions": collect_subscriptions(self.settings, self.runner)})
        raw = self.report_catalog_bindings()
        # Native work-claim bridge (Phase 1): mirror redacted local `a work`
        # declarations and `work.*` mailbox events into Hub claims/messages so
        # operators see native work in activity views. Best-effort only: a
        # bridge failure must never fail the host poll cycle. The live catalog
        # fetched above is reused so the bridge adds no extra `a list` call.
        try:
            from . import workclaim
            rows = raw if isinstance(raw, list) else (raw.get("sessions") if isinstance(raw, dict) else None)
            list_rows = {r["id"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]} if isinstance(rows, list) else None
            workclaim_summary = workclaim.sync_once(
                self.request,
                settings=self.settings,
                runner=self.runner,
                journal_dir=self.journal_dir,
                list_rows=list_rows,
            )
        except Exception as exc:
            workclaim_summary = {"ok": False, "error": (type(exc).__name__ or "Error")[:64]}
        if 'capabilities' in self.settings:
            self.request('PUT', '/api/fleet/routing/capabilities', {'needs': self.settings['capabilities'], 'max_agents': self.settings.get('max_agents', 1), 'runners': [{'engine': s.get('engine', s['service']), 'profile': s.get('profile'), 'account': s['account_label'], 'model': s['launcher_model'], 'effort': s.get('launcher_effort')} for s in self.settings['subscriptions'] if s.get('launcher_model') and s.get('admission_argv')]})
        from .launcher_recovery import settle_retired_legacy
        legacy_settled = settle_retired_legacy(self.settings.get('launcher', {}))
        reconciled = self.reconcile_launches()
        imported_reconciled = self.reconcile_imported_sessions()
        # Quota monitoring does not prove launch capability. Group by engine:
        # a monitor-only account must not disable another runnable account on
        # the same engine. Never fabricate an admission adapter or bypass it.
        configured_engines = {s.get('engine', s['service']) for s in self.settings['subscriptions']
                              if s.get('admission_argv')}
        unconfigured_engines = {s.get('engine', s['service']) for s in self.settings['subscriptions']} - configured_engines
        for engine in sorted(unconfigured_engines):
            try:
                self.request('POST', '/api/fleet/probe', {'engine': engine, 'status': 'unavailable',
                                                        'reason': 'admission_unconfigured'})
            except Exception:
                pass  # dispatch still refuses locally if health publication fails.
        offer_response = self.request("GET", "/api/fleet/offers")
        offers = offer_response.get("offers", [])
        from .account_capacity import reconcile_unstarted
        reconciled += [r for r in reconcile_unstarted(self, rows=offer_response.get('reconciliation', [])) if r['state'] in ('failed', 'exited')]
        results = [{'id': r['task_id'], 'state': 'failed', 'reason': 'legacy_native_finished'} for r in legacy_settled] + list(reconciled) + list(imported_reconciled)
        results.append({"id": "workclaim-sync", **workclaim_summary})
        for offer in offers:
            try:
                outcome = self.dispatch(offer)
                results.append(outcome)
                if outcome.get('state') == 'running':
                    try:
                        self.report_catalog_bindings()
                    except Exception as exc:
                        # A catalog/binding transport error is not a failed
                        # launch. Keep its real receipt and retry next cycle.
                        results.append({'id': offer.get('id'), 'state': 'binding_pending',
                                        'error': type(exc).__name__[:64]})
            except Exception as exc:
                from .opencode_auth import AuthBusy
                if isinstance(exc, AuthBusy):
                    results.append({'id': offer.get('id'), 'state': 'held', 'reason': 'engine_auth_busy'})
                    continue  # No claim/effect; preserve offer for the next poll.
                from .dispatch_diagnostics import classify
                workspace_available = True
                try:
                    resolve_workspace_path(self.settings, offer.get('workspace'))
                except (ValueError, OSError):
                    workspace_available = False
                diagnostic = classify(exc, workspace_available=workspace_available)
                reason = diagnostic['reason']
                # Only fixed diagnostics enter the journal; no prompt, argv,
                # account credentials or arbitrary exception text is retained.
                record = {'offer_id': offer.get('id'), 'state': 'dispatch_refused',
                          'reason': reason, 'diagnostic': diagnostic, 'settled': False}
                self._persist_launch_record(record)
                if not offer.get('task_id'):
                    try:
                        rejected = self.request('POST', '/api/fleet/launches/' + offer['id'] + '/reject', {'reason': reason, 'diagnostic': diagnostic})
                        record['settled'] = True
                        self._persist_launch_record(record)
                        results.append(rejected)
                        continue
                    except Exception:
                        pass  # A racing claim is never proof of no native effect.
                # No successful rejection receipt: keep uncertainty visible
                # locally and retry reconciliation next cycle.
                err = (type(exc).__name__ or "Error")[:64]
                results.append({"id": offer.get("id"), "state": "held",
                                "reason": reason, "stage": "dispatch_route",
                                "error": err, "diagnostic": diagnostic})
        return results


def run_cycles(settings, cycles=1, interval=60):
    if type(cycles) is not int or not 1 <= cycles <= 1000 or not 10 <= interval <= 3600:
        raise ValueError("Invalid bounded poll settings")
    dispatcher = Dispatcher(settings)
    for index in range(cycles):
        print(json.dumps({"results": dispatcher.once()}))
        if index + 1 < cycles:
            time.sleep(interval)
