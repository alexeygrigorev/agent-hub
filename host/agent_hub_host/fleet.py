"""Generic host placement client. Secrets stay in native clients, never subscription reports."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from . import bus

LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SERVICES = {"codex", "claude", "grok", "gemini", "zai", "go", "copilot"}


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
    if not isinstance(data, dict) or set(data) - {"version", "subscriptions", "workspaces", "launcher", "aplexer_bin", "capabilities", "max_agents"} or data.get("version") != 1:
        raise ValueError("Invalid subscription configuration")
    subs = data.get("subscriptions")
    if not isinstance(subs, list) or len(subs) > 128:
        raise ValueError("Invalid subscriptions list")
    seen = set()
    for sub in subs:
        if not isinstance(sub, dict) or set(sub) - {"service", "account_label", "plan", "engine", "profile", "quota_argv", "admission_argv", "launcher_provider", "launcher_model", "launcher_effort"}:
            raise ValueError("Subscription accepts metadata and local command references only")
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
    if not isinstance(workspaces, dict) or any(not isinstance(k, str) or not LABEL.fullmatch(k) or not isinstance(v, str) or not Path(v).is_absolute() for k, v in workspaces.items()):
        raise ValueError("Workspaces must map labels to absolute local paths")
    needs = data.get('capabilities', [])
    if not isinstance(needs, list) or len(needs) > 64 or any(not isinstance(n, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:_-]{0,95}', n) for n in needs) or len(set(needs)) != len(needs):
        raise ValueError('Invalid task capabilities')
    maximum = data.get('max_agents', 1)
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError('Invalid machine concurrency')
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
    threads = [threading.Thread(target=drain, args=(proc.stdout, 131072, stdout), daemon=True),
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
    from .quota_snapshot import sanitize_provider, format_iso_utc
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
    return {'status': status, 'observed_at': observed_at, 'details': clean['details'],
            'windows': {name: {k: w[k] for k in ('present', 'percent_remaining', 'reset_at', 'rolling')} for name, w in clean['windows'].items()}}


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
    for session in sessions[:200]:
        engine, profile = session.get("engine"), session.get("profile")
        matches = [s for s in settings["subscriptions"] if s.get("engine", s["service"]) == engine and s.get("profile") == profile]
        clean.append({"native_id": session["id"], "tag": session["tag"], "workspace": session.get("workspace", ""), "engine": engine,
                      "profile": profile, "account": matches[0]["account_label"] if len(matches) == 1 else None,
                      "phase": session.get("phase", "unknown"), "agent_state": session.get("reported_state"),
                      "cwd": session.get("cwd") if isinstance(session.get("cwd"), str) else None,
                      "last_activity_at": _ms_to_iso(session.get("last_activity_ms"))})
    return clean


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
        # The offer workspace is a LABEL, not a path: map it through the
        # operator-configured workspaces (label -> real local directory),
        # exactly as _route validated above. A literal offer string must
        # never become a filesystem path (label "demo" is not ./demo).
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
            cwd = None
        elif mode == "headless":
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
                       "account_label": sub["account_label"], "profile": sub.get("profile")}
            self.runner(prefix + ["submit", "--id", task_id, "--key", task_id, "--payload", json.dumps(payload)], cwd=launcher["root"])
            plan = self.runner(prefix + ["plan", "--id", task_id], cwd=launcher["root"])
            eligible = plan.get("eligible", [])
            if not isinstance(eligible, list) or not eligible or any(r.get("provider") != sub["launcher_provider"] or r.get("model") != sub["launcher_model"] or r.get("account_label") != sub["account_label"] or r.get("profile") != sub.get("profile") for r in eligible):
                raise ValueError("Maintained launcher plan refused")
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
                native_id = details.get("session_id") or details.get("unit_name") or details.get("worker_id")
                if type(details.get('exit_code')) is int:
                    result = {'state': 'exited' if details['exit_code'] == 0 else 'failed', 'confirmed_exit': True, 'native_id': native_id, 'launcher_task_id': task_id}
                elif details.get('state') == 'running' and details.get('first_action_verified') is True:
                    result = {"state": "running", "native_id": native_id, "launcher_task_id": task_id}
                else:
                    raise ValueError('Native lifecycle receipt is incomplete')
            if not isinstance(native_id, str) or not native_id:
                raise ValueError("No confirmed native execution receipt")
        except Exception:
            # Effect may have happened: hold capacity and require reconciliation.
            self.request("POST", path + "/result", {"state": "uncertain"})
            return {"id": offer["id"], "state": "uncertain"}
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
        if 'capabilities' in self.settings:
            self.request('PUT', '/api/fleet/routing/capabilities', {'needs': self.settings['capabilities'], 'max_agents': self.settings.get('max_agents', 1), 'runners': [{'engine': s.get('engine', s['service']), 'profile': s.get('profile'), 'account': s['account_label'], 'model': s['launcher_model'], 'effort': s.get('launcher_effort')} for s in self.settings['subscriptions'] if s.get('launcher_model') and s.get('admission_argv')]})
        offers = self.request("GET", "/api/fleet/offers").get("offers", [])
        results = []
        for offer in offers:
            try:
                results.append(self.dispatch(offer))
            except Exception:
                results.append({"id": offer.get("id"), "state": "held"})
        return results


def run_cycles(settings, cycles=1, interval=60):
    if type(cycles) is not int or not 1 <= cycles <= 1000 or not 10 <= interval <= 3600:
        raise ValueError("Invalid bounded poll settings")
    dispatcher = Dispatcher(settings)
    for index in range(cycles):
        print(json.dumps({"results": dispatcher.once()}))
        if index + 1 < cycles:
            time.sleep(interval)
