"""Single source of truth for Hub accounts on hosts (intake 7fd96369).

The canonical account catalog lives in config/model-routing.json `accounts`
(one definition per account: harness, provider, account label, canonical
aplexer profile, credential location/mode, quota probe selector). This module
renders host files from that catalog and checks them for drift:

- subscriptions.json `subscriptions` entries (catalog-owned identity fields:
  service, engine, profile, quota_argv; host-local fields such as
  admission_argv, launcher_* and startup_argv are preserved, never invented),
- the `# BEGIN HUB MANAGED ENGINES` block of the aplexer config (hub-*
  profiles for managed-bundle accounts, rendered through provisioning so the
  format always matches what bundle installation writes),
- Hub fleet_routes for this machine, and Hub's own quota reading per account
  (Hub reads quota from the providers; this host runs no quota CLI).

Only stdlib is used. All rendering is atomic (temp file + os.replace via
private_fs.atomic_write). Nothing here deletes operator data: unknown
subscriptions and non-catalog profiles are preserved and reported, never
removed. Legacy labels (e.g. urgent-muse for shared-go) match by canonical
identity; renaming labels is owned by the relabel intake, not by sync.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import private_fs
from .model_contract import canonical_account_label

LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
HUB_PROFILE = re.compile(r"hub-[a-z0-9-]+\Z")
CREDENTIAL_MODES = ("managed-bundle", "host-home")



def policy_accounts(policy=None):
    """Validated canonical account catalog (list of dicts)."""
    if policy is None:
        from .model_contract import _policy_document
        policy = _policy_document()
    accounts = policy.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("Account catalog missing: config/model-routing.json `accounts` is empty")
    seen = set()
    for entry in accounts:
        if not isinstance(entry, dict):
            raise ValueError("Account catalog entry must be an object")
        account = entry.get("account")
        if not isinstance(account, str) or not LABEL.fullmatch(account):
            raise ValueError("Account catalog entry needs a valid `account` label")
        if account in seen:
            raise ValueError("Duplicate account in catalog: " + account)
        seen.add(account)
        if not isinstance(entry.get("harness"), str) or not LABEL.fullmatch(entry["harness"]):
            raise ValueError("Account %s needs a valid `harness`" % account)
        if not isinstance(entry.get("provider"), str) or not entry["provider"]:
            raise ValueError("Account %s needs a `provider`" % account)
        profile = entry.get("aplexer_profile")
        if profile is not None and (not isinstance(profile, str) or not HUB_PROFILE.fullmatch(profile)):
            raise ValueError("Account %s needs `aplexer_profile` null or hub-<name>" % account)
        credential = entry.get("credential")
        if not isinstance(credential, dict) or credential.get("mode") not in CREDENTIAL_MODES:
            raise ValueError("Account %s needs a credential mode (managed-bundle/host-home)" % account)
        if credential["mode"] == "managed-bundle":
            bundle = credential.get("bundle")
            if not isinstance(bundle, str) or not LABEL.fullmatch(bundle):
                raise ValueError("Account %s needs a credential bundle label" % account)
        else:
            path = credential.get("path")
            if not isinstance(path, str) or not path.startswith("~/") or "\0" in path:
                raise ValueError("Account %s needs a credential home path" % account)
        # Legacy catalog field: quota is read by Hub (intake 5814e109), never
        # by a host-side probe. Accepted for compatibility, never executed.
        probe = entry.get("quota_probe")
        if probe is not None and (not isinstance(probe, list) or any(not isinstance(x, str) or not x or "\0" in x for x in probe)):
            raise ValueError("Account %s has an invalid legacy `quota_probe`" % account)
        if set(entry) - {"account", "harness", "provider", "aplexer_profile", "credential", "quota_probe"}:
            raise ValueError("Account %s has unknown catalog fields" % account)
    return accounts


def _catalog_index(accounts):
    return {a["account"]: a for a in accounts}


def diff_subscriptions(settings, accounts):
    """Merge catalog identity into settings. Returns (entries, changes).

    Never renames a legacy label in place and never drops unknown entries:
    an entry whose label canonicalizes to a catalog account keeps its label
    and gains the catalog-owned fields; a catalog account with no entry is
    appended under its canonical label.
    """
    index = _catalog_index(accounts)
    entries = [dict(s) for s in settings.get("subscriptions", [])]
    changes = []
    matched = set()
    for sub in entries:
        canonical = canonical_account_label(sub.get("account_label"))
        account = next((a for a in accounts if a["account"] == canonical), None)
        if account is None:
            continue
        matched.add(account["account"])
        wanted = {
            "service": account["provider"],
            "engine": account["harness"],
            "profile": account["aplexer_profile"],
            "quota_argv": list(account["quota_probe"]),
        }
        for key, value in wanted.items():
            if sub.get(key) != value:
                changes.append("subscriptions: %s: set %s" % (sub.get("account_label"), key))
                sub[key] = value
    for account in accounts:
        if account["account"] not in matched:
            entries.append({
                "account_label": account["account"],
                "service": account["provider"],
                "engine": account["harness"],
                "profile": account["aplexer_profile"],
                "quota_argv": list(account["quota_probe"]),
            })
            changes.append("subscriptions: %s: add entry" % account["account"])
    # Load-time validation rejects duplicate labels; keep that invariant here.
    labels = [s.get("account_label") for s in entries]
    if len(set(labels)) != len(labels):
        raise ValueError("Refusing to render duplicate subscription labels")
    canonical_routes = set()
    for sub in entries:
        key = (canonical_account_label(sub.get("account_label")), sub.get("engine", sub.get("service")), sub.get("profile"))
        if key in canonical_routes:
            raise ValueError("Refusing to render an ambiguous canonical route: " + str(key[0]))
        canonical_routes.add(key)
    return entries, changes


# Native (host-home) catalog accounts a host renders by itself when the engine
# CLI and its login are present locally (intake 608a2f9a). Claude is covered by
# the managed hub-claude bundle and Grok is a disabled provider, so neither is
# auto-rendered.
NATIVE_BINARIES = {"zcodex": ("zcodex",), "antigravity": ("agy", "antigravity"), "opencode": ("opencode",)}
# User-scope install locations outside PATH (scripts/windows/add-hub-engines.ps1).
NATIVE_BINARY_DIRS = ("~/.local/bin", "~/bin")
WINDOWS_BINARY_DIRS = (("APPDATA", "npm"), ("LOCALAPPDATA", "Programs/Antigravity/bin"), ("LOCALAPPDATA", "Programs/agy"))
NATIVE_LAUNCHER = {"zai": "zai", "gemini": "antigravity", "go": "opencode"}
NATIVE_CREDENTIAL_MARKERS = {"go": "auth.json"}


def _default_launcher_model(provider, harness):
    from .model_contract import policy_models
    for model in policy_models():
        if model.get("provider") == provider and model.get("engine") == harness:
            return model
    return None


def _find_binary(names, which=shutil.which, home=None):
    dirs = [Path(expand_home(d, home)) for d in NATIVE_BINARY_DIRS]
    if sys.platform == "win32":
        dirs += [Path(os.environ[var]) / sub for var, sub in WINDOWS_BINARY_DIRS if os.environ.get(var)]
    suffixes = (".exe", ".cmd", "") if sys.platform == "win32" else ("",)
    for name in names:
        found = which(name)
        if found:
            return found
        for directory in dirs:
            for suffix in suffixes:
                candidate = directory / (name + suffix)
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    return None


def native_subscriptions(settings, accounts=None, which=shutil.which, home=None, python=None):
    """Subscription entries for catalog host-home accounts usable on this host.

    An account is rendered only when no configured entry already maps to it,
    its provider is enabled, the engine CLI resolves locally and its
    credential home exists. Operator entries always win; nothing is written.
    """
    if accounts is None:
        accounts = policy_accounts()
    from .native_admission import routing_rules
    disabled = set(routing_rules().get("disabled_providers", []))
    configured = {canonical_account_label(s.get("account_label")) for s in settings.get("subscriptions", [])
                  if isinstance(s, dict)}
    python = python or sys.executable
    subs = []
    for account in accounts:
        provider, harness = account["provider"], account["harness"]
        if (account["credential"]["mode"] != "host-home" or provider not in NATIVE_LAUNCHER
                or provider in disabled or account["account"] in configured):
            continue
        binary = _find_binary(NATIVE_BINARIES.get(harness, (harness,)), which=which, home=home)
        credential = Path(expand_home(account["credential"]["path"], home))
        marker = NATIVE_CREDENTIAL_MARKERS.get(provider)
        if not binary or not credential.is_dir() or (marker and not (credential / marker).is_file()):
            continue
        sub = {
            "service": provider,
            "account_label": account["account"],
            "engine": harness,
            "profile": None,
            "admission_argv": [python, "-m", "agent_hub_host.native_admission", "--service", provider,
                               "--account", account["account"], "--engine", harness],
            "launcher_provider": NATIVE_LAUNCHER[provider],
        }
        model = _default_launcher_model(provider, harness)
        if model:
            sub["launcher_model"] = model["model"]
            if model.get("effort"):
                sub["launcher_effort"] = model["effort"]
        if harness == "zcodex":
            # Goal routes need a trusted local start command (same as RMTHZ).
            sub["startup_argv"] = [binary, "--dangerously-bypass-approvals-and-sandbox",
                                   "-c", "check_for_update_on_startup=false"]
        subs.append(sub)
    return subs


def read_text(path, limit=262144):
    try:
        return private_fs.read_bytes(Path(path), limit).decode()
    except FileNotFoundError:
        return None


def sync_settings(settings_path, accounts, write=False):
    """Render subscriptions.json from the catalog. Returns a result dict."""
    from . import fleet
    path = Path(settings_path) if settings_path else fleet.subscriptions_path()
    settings = fleet.load_settings(str(path))
    entries, changes = diff_subscriptions(settings, accounts)
    result = {"settings_path": str(path), "changes": changes, "wrote": False,
              "subscriptions": len(entries)}
    if changes and write:
        settings["subscriptions"] = entries
        private_fs.atomic_write(path, json.dumps(settings, indent=2).encode() + b"\n", 65536)
        result["wrote"] = True
    return result


def _managed_entries(text):
    """Parse hub-* profiles out of an aplexer config text: {name: engine}."""
    found = {}
    if not text:
        return found
    for match in re.finditer(r"^\s*\[profiles\.(hub-[^\].\s]+)\]", text, re.M):
        name = match.group(1)
        engine = None
        tail = text[match.end():match.end() + 800]
        engine_match = re.search(r"^engine\s*=\s*\"([^\"]+)\"", tail, re.M)
        if engine_match:
            engine = engine_match.group(1)
        found[name] = engine
    return found


def installed_bundles():
    """Managed engine bundles installed on this host: {label: provider}."""
    from . import provisioning
    state = provisioning.read_json(provisioning.root() / "state.json", [])
    result = {}
    if isinstance(state, list):
        for item in state:
            if isinstance(item, dict) and isinstance(item.get("label"), str):
                result[item["label"]] = item.get("provider")
    return result


def sync_aplexer_profiles(aplexer_path, accounts, write=False):
    """Render the managed hub-* profile block from catalog + installed bundles.

    Installed managed bundles are rendered through provisioning (identical
    format to bundle installation). Profiles already present for catalog
    managed accounts without an installed bundle are preserved verbatim by
    re-rendering their label/provider through the same path once the bundle
    record is faked from the catalog... instead they are left untouched:
    only installed bundles are (re)rendered, everything else is preserved.
    Returns a result dict.
    """
    from . import provisioning
    path = Path(aplexer_path) if aplexer_path else provisioning.profile_path()
    text = read_text(path)
    if text is None:
        text = "version = 1\n"
    if text.count(provisioning.BEGIN) != text.count(provisioning.END) or text.count(provisioning.BEGIN) > 1:
        raise ValueError("Invalid managed profile block")
    installed = installed_bundles()
    managed_labels = {a["account"] for a in accounts if a["credential"]["mode"] == "managed-bundle"}
    bundles = [{"label": label, "provider": installed[label]}
               for label in sorted(set(installed) & managed_labels) if installed.get(label)]
    have = _managed_entries(text)
    result = {"aplexer_path": str(path), "changes": [], "wrote": False,
              "managed": ["hub-" + b["label"] for b in bundles]}
    if not bundles:
        return result
    # Re-render only when the desired set differs from the block text.
    # provisioning.profiles() refuses operator collisions and writes
    # atomically; pass exactly the installed catalog bundles.
    desired = {("hub-" + b["label"]): b for b in bundles}
    same = all(name in have for name in desired)
    if same:
        # Engines may still differ (e.g. after a catalog harness change).
        from .provisioning import PACKAGES
        for name, bundle in desired.items():
            engine = PACKAGES.get(bundle.get("provider"), (None, None, None))[2]
            if have.get(name) != engine:
                same = False
                result["changes"].append("aplexer: %s: set engine %s" % (name, engine))
    if not same and not result["changes"]:
        result["changes"].append("aplexer: render managed profiles: " + ", ".join(sorted(desired)))
    if result["changes"] and write:
        provisioning.profiles(bundles, path=path)
        result["wrote"] = True
    return result


def resolve_machine(machines, hostname=None, explicit=None):
    """Pick the machine id for route comparison. Returns id or None."""
    by_id = {}
    for machine in machines or []:
        if isinstance(machine, dict) and machine.get("id"):
            by_id[machine["id"]] = machine
    if explicit:
        if explicit in by_id:
            return by_id[explicit].get("canonical_id") or explicit
        for machine in by_id.values():
            if machine.get("name") == explicit or machine.get("hostname") == explicit:
                return machine.get("canonical_id") or machine.get("id")
        return None
    host = hostname or socket.gethostname()
    hits = [m for m in by_id.values() if m.get("hostname") == host]
    canonical = {m.get("canonical_id") or m.get("id") for m in hits}
    if len(canonical) == 1:
        return next(iter(canonical))
    return None


def hub_quota_states(pools):
    """Map Hub's /api/quota pools to {account: (ok, detail)}; fresh means Hub read it."""
    states = {}
    for pool in pools or []:
        if isinstance(pool, dict) and isinstance(pool.get("account"), str):
            state = pool.get("state")
            states[pool["account"]] = (state == "fresh", "hub quota %s" % state)
    return states


def expand_home(path, home=None):
    if home is None:
        return os.path.expanduser(path)
    if path == "~":
        return home
    if path.startswith("~/"):
        return home.rstrip("/") + path[1:]
    return path


def doctor_command(settings_path=None, aplexer_path=None, fleet_snapshot_path=None,
                   machine=None, skip_quse=False, probe_timeout=60, runner=None,
                   home=None, managed_root=None):
    """Run the full drift check. Returns a JSON-safe result dict."""
    from . import fleet
    from . import provisioning
    accounts = policy_accounts()
    settings = fleet.load_settings(settings_path)
    aplexer_file = Path(aplexer_path) if aplexer_path else provisioning.profile_path()
    aplexer_text = read_text(aplexer_file)
    if fleet_snapshot_path:
        snapshot = json.loads(Path(fleet_snapshot_path).read_text(encoding="utf-8"))
    else:
        from . import session_control
        snapshot = session_control.request("GET", "/api/fleet")
    machines = snapshot.get("machines", []) if isinstance(snapshot, dict) else []
    machine_id = resolve_machine(machines, explicit=machine)
    if machine_id is None:
        raise ValueError("Cannot identify this host in the fleet registry; pass --machine explicitly")
    routes = [r for r in (snapshot.get("routes", []) if isinstance(snapshot, dict) else [])
              if isinstance(r, dict) and (r.get("machine_id") == machine_id)]
    probe_results = None
    if not skip_quse:
        # Hub owns quota (intake 5814e109): read its per-account state instead of probing here.
        if runner is not None:
            quota = runner()
        else:
            from . import session_control
            quota = session_control.request("GET", "/api/quota")
        states = hub_quota_states(quota.get("pools") if isinstance(quota, dict) else None)
        probe_results = {a["account"]: states.get(a["account"], (False, "no Hub quota reading")) for a in accounts}
    if aplexer_text is None:
        aplexer_problems = ["aplexer: config file is missing: %s" % aplexer_file]
        problems_text = None
    else:
        aplexer_problems = []
        problems_text = aplexer_text
    problems = check_drift(accounts, settings.get("subscriptions", []), problems_text,
                           routes, probe_results=probe_results, home=home,
                           managed_root=managed_root) + aplexer_problems
    return {"machine": machine_id, "problems": problems,
            "checks": {"subscriptions": len(settings.get("subscriptions", [])),
                       "routes": len(routes),
                       "accounts": len(accounts),
                       "aplexer_managed": sorted(_managed_entries(aplexer_text or "")),
                       "probes": ("skipped" if skip_quse else
                                  {label: ("ok" if probe_results.get(label, (False,))[0] else "failed")
                                   for label in [a["account"] for a in accounts]})}}
    if home is None:
        return os.path.expanduser(path)
    if path == "~":
        return home
    if path.startswith("~/"):
        return home.rstrip("/") + path[1:]
    return path


def check_drift(accounts, subscriptions, aplexer_text, routes, probe_results=None, home=None, managed_root=None):
    """Flag mismatches between catalog, host files, routes and probes."""
    problems = []
    index = _catalog_index(accounts)
    subs = list(subscriptions or [])
    by_canonical = {}
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        key = (canonical_account_label(sub.get("account_label")), sub.get("engine", sub.get("service")), sub.get("profile"))
        by_canonical.setdefault(key, []).append(sub)
    for account in accounts:
        label = account["account"]
        matches = [s for s in subs if canonical_account_label(s.get("account_label")) == label]
        if not matches:
            problems.append("subscriptions: account %s has no host entry" % label)
            continue
        if len(matches) > 1:
            problems.append("subscriptions: account %s has %d host entries" % (label, len(matches)))
        sub = matches[0]
        for key, want in (("service", account["provider"]), ("engine", account["harness"]),
                          ("profile", account["aplexer_profile"]), ("quota_argv", account["quota_probe"])):
            if sub.get(key) != want:
                problems.append("subscriptions: %s: %s drift (want %s)" % (
                    sub.get("account_label"), key, json.dumps(want)))
        credential = account["credential"]
        if credential["mode"] == "managed-bundle":
            root = Path(managed_root) if managed_root else None
            if root is None:
                from . import provisioning
                root = provisioning.root()
            hits = [p for p in root.glob(credential["bundle"] + "-*") if p.is_dir()] if root.is_dir() else []
            if not hits:
                problems.append("credential: managed bundle %s for %s is not installed" % (credential["bundle"], label))
        else:
            if not Path(expand_home(credential["path"], home)).is_dir():
                problems.append("credential: home path %s for %s is missing" % (credential["path"], label))
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        key = (canonical_account_label(route.get("account")), route.get("engine"), route.get("profile"))
        hits = by_canonical.get(key, [])
        if len(hits) != 1:
            problems.append("routes: %s/%s/%s has %d host matches" % (
                route.get("engine"), route.get("account"), route.get("profile"), len(hits)))
    if aplexer_text is not None:
        have = _managed_entries(aplexer_text)
        for account in accounts:
            if account["credential"]["mode"] != "managed-bundle":
                continue
            name = "hub-" + account["account"]
            if name not in have:
                problems.append("aplexer: managed profile [%s] is missing" % name)
        for name in have:
            if name.startswith("hub-") and name[4:] not in index:
                problems.append("aplexer: [%s] is not a catalog managed account" % name)
    for account in accounts:
        if probe_results is not None and account["account"] in probe_results:
            ok, detail = probe_results[account["account"]]
            if not ok:
                problems.append("quota: %s %s" % (account["account"], detail))
    return problems
