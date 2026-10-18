"""Generic host placement client. Secrets stay in native clients, never subscription reports."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import uuid
import subprocess
import time
from typing import Any

from . import bus

LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SERVICES = {"codex", "claude", "grok", "gemini", "zai", "go", "copilot"}
WORKSPACE_SEG = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def absolute_workspace_ok(value):
    """Bounded canonical absolute POSIX workspace identifier.

    Mirrors the Hub server validator: no NUL/control chars, no backslashes,
    no `//`, no trailing slash, no `.`/`..` segments, segments confined to
    `[A-Za-z0-9._-]{1,64}`, total length capped at 512. Platform-neutral on
    purpose (Windows stays offline; POSIX acceptance now, legacy source
    preserved everywhere).
    """
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if not value.startswith('/') or '\\' in value or '\x00' in value:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    parts = value.split('/')
    if parts[0] != '' or len(parts) < 2:
        return False
    return all(seg and seg not in ('.', '..') and WORKSPACE_SEG.fullmatch(seg) for seg in parts[1:])


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


def load_settings(path=None):
    """Explicit non-secret machine configuration, including several accounts per service."""
    path = Path(path) if path else subscriptions_path()
    if path.stat().st_size > 65536:
        raise ValueError("Subscription configuration exceeds 64 KiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"version", "subscriptions", "workspaces", "launcher", "aplexer_bin", "capabilities", "max_agents", "cleanup_roots", "native_start_argv", "role_consumer", "role_config"} or data.get("version") != 1:
        raise ValueError("Invalid subscription configuration")
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
            not isinstance(r, str) or not r or '\x00' in r or not r.startswith('/') for r in roots):
        raise ValueError('Invalid cleanup roots allowlist')
    if len(set(roots)) != len(roots):
        raise ValueError('Invalid cleanup roots allowlist')
    return data


def run_json(argv, *, payload=None, cwd=None, timeout=30):
    """No shell, no provider stdout/stderr in exceptions or logs."""
    import threading
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    overflow = threading.Event()
    stdout = bytearray()
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
    stdout_limit = 8 * 1024 * 1024 if list(argv[-2:]) == ["list", "--json"] else 131072
    threads = [threading.Thread(target=drain, args=(proc.stdout, stdout_limit, stdout), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 16384), daemon=True),
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
            raise RuntimeError('Local command failed or exceeded output limit')
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


def quota_metadata(raw, service):
    """Reuse the maintained quota contract; never reimplement its failure/absence semantics."""
    from .quota_snapshot import sanitize_provider, format_iso_utc, COLLECTOR_VERSION_REGEX
    route = raw.get(service, raw) if isinstance(raw, dict) else {}
    if not isinstance(route, dict): route = {}
    details = route.get('details') if isinstance(route.get('details'), dict) else {}
    now = dt.datetime.now(dt.timezone.utc)
    clean = sanitize_provider(service, route, now)
    status = 'ok' if clean['status'] == 'ok' else 'unknown'
    explicit_timestamp = 'observed_at' in route or 'observed_at' in details
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
        reports.append({"service": service, "account": sub["account_label"], "plan": plan, "quota": quota_metadata(raw, service)})
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


def session_metadata(raw, settings):
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
        clean.append({"native_id": session["id"], "tag": session["tag"], "workspace": session.get("workspace", ""), "engine": engine,
                      "profile": profile, "account": matches[0]["account_label"] if len(matches) == 1 else None,
                      "phase": session.get("phase", "unknown"), "agent_state": session.get("reported_state"),
                      "cwd": session.get("cwd") if isinstance(session.get("cwd"), str) else None,
                      "last_activity_at": _ms_to_iso(session.get("last_activity_ms")),
                      "created_at_ms": created_at_ms})
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
    def __init__(self, settings, request=bus._do_request, runner=run_json):
        self.settings, self.request, self.runner = settings, request, runner

    def _route(self, offer):
        matches = [s for s in self.settings["subscriptions"] if s["account_label"] == offer.get("account") and s.get("engine", s["service"]) == offer.get("engine") and s.get("profile") == offer.get("profile")]
        if len(matches) != 1 or offer.get("workspace") not in self.settings.get("workspaces", {}):
            raise ValueError("Local account/profile/workspace is not configured")
        sub = matches[0]
        if not sub.get("admission_argv"):
            raise ValueError("Maintained fresh admission adapter is not configured")
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

    def dispatch(self, offer):
        sub = self._route(offer)
        selection = offer.get('selection')
        if offer.get('task_id'):
            if not isinstance(offer.get('needs'), list) or not set(offer['needs']).issubset(self.settings.get('capabilities', [])):
                raise ValueError('Required capabilities are no longer available locally')
            if not isinstance(selection, dict) or selection.get('model') != sub.get('launcher_model') or selection.get('effort') != sub.get('launcher_effort'):
                raise ValueError('Selected model/effort is not configured locally')
        gate = self.runner(sub["admission_argv"], payload={k: offer.get(k) for k in ["mode", "engine", "profile", "account", "workspace", "selection"]})
        if gate.get("launch_allowed") is not True or gate.get("profile_supported") is not True or gate.get("account_label") != sub["account_label"]:
            raise ValueError("Fresh admission refused")
        if offer.get('task_id') and (gate.get('model_supported') is not True or gate.get('model') != selection['model'] or gate.get('effort') != selection['effort']):
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
        workspace = Path(self.settings["workspaces"][offer["workspace"]]).resolve(strict=True)
        target_cwd = resolve_offer_cwd(workspace, offer.get("cwd"))
        native = self.settings.get("aplexer_bin", "aplexer")
        if mode == "interactive":
            if offer.get("prompt"):
                raise ValueError("Interactive task injection is unsupported; start an empty native session")
            argv = [native, "start", "--workspace", str(workspace), "--tag", "hub-" + offer["id"], "--engine", offer["engine"], "--memory", "1500M", "--pids", "256", "--json"]
            if offer.get("profile"):
                argv += ["--profile", offer["profile"]]
            if target_cwd != workspace:
                argv += ["--cwd", str(target_cwd)]
            if sub.get("startup_argv"):
                launch = sub["startup_argv"]
                if not isinstance(launch, list) or not launch or any(not isinstance(a, str) or "\0" in a for a in launch):
                    raise ValueError("Invalid trusted startup argv")
                argv += ["--"] + launch
            prefix = sub.get("native_start_argv") or self.settings.get("native_start_argv")
            if prefix is not None:
                prefix = validate_native_start_argv(prefix)
                argv = list(prefix) + argv
            cwd = None
        elif mode == "headless":
            # Provider profiles are not launcher admission profiles. This
            # backend currently qualifies only the default provider profile.
            if sub.get("profile") is not None:
                raise ValueError("Headless provider profile is not qualified")
            launcher = self.settings.get("launcher", {})
            if not all(launcher.get(k) for k in ["argv", "root", "config_dir"]) or not sub.get("launcher_provider") or not sub.get("launcher_model"):
                raise ValueError("Maintained launcher route is not configured")
            # Only the maintained launcher may run models. argv is a trusted machine-local CLI prefix.
            prefix = launcher["argv"] + ["--config-dir", launcher["config_dir"]]
            task_id = "hub-" + offer["id"]
            goal = offer["prompt"]
            result_path = workspace / '.local' / 'hub-results' / (offer['id'] + '.json')
            if offer.get('task_id'):
                result_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                goal += '\nWrite a final JSON object to ' + str(result_path) + ': {"status":"done|failed|waiting-on-human|stuck","report":"sanitized final report","artifacts":[{"label":"text","text":"sanitized text"}]}. No secrets, codes, tokens or URLs. Do not write raw screenshots; keep them private for human review locally.'
            payload = {"goal": goal, "owner": "hub-fleet", "cwd": str(target_cwd), "timeout": 1800,
                       "model_requirements": {"providers": [sub["launcher_provider"]], "models": [sub["launcher_model"]]},
                       "account_label": sub["account_label"], "profile": "model-task"}
            self.runner(prefix + ["submit", "--id", task_id, "--key", task_id, "--payload", json.dumps(payload), "--json"], cwd=launcher["root"])
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
                raise ValueError("Maintained launcher plan refused")
            # OpenCode and Antigravity use the maintained native backend:
            # real engine/UUID plus validated first action for Hub binding.
            # Other providers retain their existing task-unit semantics.
            if sub.get("launcher_provider") in {"opencode", "antigravity", "zai"}:
                argv = prefix + ["run", "--backend", "aplexer",
                                 "--id", task_id, "--cwd", str(workspace),
                                 "--tmpdir", str(workspace / ".local" / "tmp")]
            else:
                argv = prefix + ["run", "--backend", "task-units", "--as-controller", "--id", task_id, "--cwd", str(target_cwd), "--tmpdir", str(workspace / ".local" / "tmp")]
            cwd = launcher["root"]
        else:
            raise ValueError("Unsupported launch mode")
        path = "/api/fleet/launches/" + offer["id"]
        # An uncertain response here leaves the server reservation intact and performs no native effect.
        self.request("POST", path + "/claim", {"gate_allowed": True, "profile_supported": True, "observed_at": at, "account": sub["account_label"]})
        try:
            receipt = self.runner(argv, cwd=cwd, timeout=120)
            if mode == "interactive":
                native_id = receipt.get("id")
                if receipt.get("workspace") != str(workspace) or receipt.get("tag") != "hub-" + offer["id"] or receipt.get('engine') != offer['engine'] or receipt.get('profile') != offer.get('profile') or receipt.get('phase') != 'running':
                    raise ValueError("Native receipt mismatch")
                if target_cwd != workspace and not self._verify_native_cwd(native, native_id, target_cwd, receipt):
                    raise ValueError("Native cwd mismatch")
                result = {"state": "running", "native_id": native_id}
            else:
                details = receipt.get("receipt", {})
                if sub.get("launcher_provider") in {"opencode", "antigravity", "zai"}:
                    # Opencode binds real M12 sessions only: a genuine native
                    # UUID is required; task-units unit_name/worker_id names
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
                    if (details.get("engine") != "zcodex" or offer.get("engine") != "zcodex"
                            or details.get("workspace") != str(workspace)
                            or details.get("tag") != "task-" + task_id):
                        raise ValueError("Native ZCodex receipt mismatch")
                if type(details.get('exit_code')) is int:
                    result = {'state': 'exited' if details['exit_code'] == 0 else 'failed', 'confirmed_exit': True, 'native_id': native_id, 'launcher_task_id': task_id}
                elif details.get('state') == 'running' and details.get('first_action_verified') is True:
                    result = {"state": "running", "native_id": native_id, "launcher_task_id": task_id}
                else:
                    raise ValueError('Native lifecycle receipt is incomplete')
            if not isinstance(native_id, str) or not native_id:
                raise ValueError("No confirmed native execution receipt")
        except Exception as exc:
            # Effect may have happened: hold capacity and require reconciliation.
            # Bounded private diagnostic only (type + stage, never argv/goal/
            # tokens/stdout). Returned locally and reported as uncertain reason
            # so real runtime start failures stay visible; server authority
            # unchanged (still uncertain, never claimed running).
            err = (type(exc).__name__ or "Error")[:64]
            stage = ("dispatch_headless" if mode == "headless" else "dispatch_interactive")[:32]
            reason = ("%s:%s" % (stage, err))[:120]
            try:
                self.request("POST", path + "/result", {"state": "uncertain"})
            except Exception:
                pass
            return {"id": offer["id"], "state": "uncertain", "reason": reason,
                    "stage": stage, "error": err}
        if offer.get('task_id') and result.get('confirmed_exit'):
            from .routing import read_text
            try:
                if result_path.is_symlink(): raise ValueError('Refuse linked result file')
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
                return {'id': offer['id'], 'state': report['status']}
            # Human waits retain reservations even when the reporting process exits.
            return {'id': offer['id'], 'state': report['status']}
        self.request("POST", path + "/result", result)
        return {"id": offer["id"], **result}

    def once(self):
        from . import api, system
        info = system.get_system_info()
        caps = info['capabilities']
        caps.update(engines=sorted({s.get('engine', s['service']) for s in self.settings['subscriptions']}),
                    workspaces=sorted(self.settings.get('workspaces', {})), cpu_count=os.cpu_count())
        caps['fleet_modes'] = ['interactive'] + (['headless'] if self.settings.get('launcher') else [])
        self.request('POST', '/api/machines/register', info)
        hb = api.get_heartbeat_payload()
        allowed = False
        # This is only a scheduling projection. dispatch() repeats the actual mode/account gate.
        for sub in self.settings['subscriptions']:
            if not sub.get('admission_argv'):
                continue
            try:
                gate = self.runner(sub['admission_argv'], payload={'mode': 'interactive', 'engine': sub.get('engine', sub['service']), 'profile': sub.get('profile'), 'account': sub['account_label'], 'workspace': next(iter(self.settings.get('workspaces', {})), None)})
                at = dt.datetime.fromisoformat(gate['observed_at'].replace('Z', '+00:00'))
                age = (dt.datetime.now(dt.timezone.utc) - at).total_seconds()
                allowed |= gate.get('launch_allowed') is True and gate.get('profile_supported') is True and gate.get('account_label') == sub['account_label'] and 0 <= age <= 30
            except Exception:
                pass
        hb['gate'] = {'launch_allowed': allowed, 'reason': 'fleet local admission' if allowed else 'fleet admission unavailable', 'at': utcnow()}
        self.request('POST', '/api/host/poll', hb)
        self.request("PUT", "/api/fleet/subscriptions", {"subscriptions": collect_subscriptions(self.settings, self.runner)})
        raw = self.runner([self.settings.get("aplexer_bin", "aplexer"), "list", "--json"])
        self.request("PUT", "/api/fleet/sessions", {"sessions": session_metadata(raw, self.settings)})
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
        if 'capabilities' in self.settings:
            self.request('PUT', '/api/fleet/routing/capabilities', {'needs': self.settings['capabilities'], 'max_agents': self.settings.get('max_agents', 1), 'runners': [{'engine': s.get('engine', s['service']), 'profile': s.get('profile'), 'account': s['account_label'], 'model': s['launcher_model'], 'effort': s.get('launcher_effort')} for s in self.settings['subscriptions'] if s.get('launcher_model') and s.get('admission_argv')]})
        offers = self.request("GET", "/api/fleet/offers").get("offers", [])
        results = []
        for offer in offers:
            try:
                results.append(self.dispatch(offer))
            except Exception as exc:
                # Route/claim/admission refusal: no native effect. Bounded
                # classification only (type + stage, never argv/goal/secrets)
                # so held-offers stay distinguishable in local counts.
                err = (type(exc).__name__ or "Error")[:64]
                reason = ("dispatch_refused:%s" % err)[:120]
                results.append({"id": offer.get("id"), "state": "held",
                                "reason": reason, "stage": "dispatch_route",
                                "error": err})
        return results


def run_cycles(settings, cycles=1, interval=60):
    if type(cycles) is not int or not 1 <= cycles <= 1000 or not 10 <= interval <= 3600:
        raise ValueError("Invalid bounded poll settings")
    dispatcher = Dispatcher(settings)
    for index in range(cycles):
        print(json.dumps({"results": dispatcher.once()}))
        if index + 1 < cycles:
            time.sleep(interval)
