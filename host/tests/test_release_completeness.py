"""Host release completeness + relabel-safe routes + capability PUT gating.

AC1: every release-relative path read by host code must be in the release
manifest (scripts/host-release-manifest.json), and verify-host-release.py
must refuse an incomplete tree.
AC2: Dispatcher._route() matches the Hub-canonical and legacy aliases of an
account label in both directions; listing both sides is rejected at load.
AC3: once() never PUTs routing capabilities without an explicit max_agents,
so a degraded settings state cannot overwrite the real host concurrency.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_hub_host import fleet
from agent_hub_host.model_contract import account_aliases, canonical_account_label

REPO = Path(__file__).resolve().parents[2]
HOST_PKG = REPO / "host" / "agent_hub_host"
MANIFEST = REPO / "scripts" / "host-release-manifest.json"
VERIFY = REPO / "scripts" / "verify-host-release.py"

RELEASE_READ = re.compile(r"parents\[2\]\s*((?:/\s*['\"][^'\"]+['\"]\s*)+)")
SEGMENT = re.compile(r"['\"]([^'\"]+)['\"]")
# Any release-root anchor must go through the inline form above; an
# intermediate variable would bypass the manifest scan.
RELEASE_ROOT = re.compile(r"parents\[2\]|parent\.parent\.parent")


def release_relative_paths():
    found = set()
    anchor_uses = 0
    for path in sorted(HOST_PKG.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        anchor_uses += len(RELEASE_ROOT.findall(text))
        for match in RELEASE_READ.finditer(text):
            segments = SEGMENT.findall(match.group(1))
            if segments:
                found.add("/".join(segments))
    return found, anchor_uses


def go_settings(tmp, label):
    return {
        "version": 1,
        "workspaces": {"demo": str(tmp)},
        "capabilities": [],
        "max_agents": 100,
        "subscriptions": [
            {"service": "go", "engine": "opencode", "account_label": label,
             "profile": None, "admission_argv": ["maintained-gate"]},
        ],
    }


def fake_poll():
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET" and path == "/api/fleet/offers":
            return {"offers": []}
        if method == "GET" and path.startswith("/api/sessions/host-bindings"):
            return {"bindings": []}
        return {}

    def runner(argv, **kw):
        return []

    return calls, request, runner


class ReleaseCompletenessTests(unittest.TestCase):
    def test_manifest_covers_every_release_relative_read(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        trees = manifest.get("hashed_trees", manifest.get("packages", []))
        found, anchor_uses = release_relative_paths()
        # Every release-root anchor must be visible to the scan: no indirect
        # `root = ...parents[2]` variable may bypass the manifest.
        direct = sum(len(RELEASE_READ.findall((HOST_PKG / name).read_text(encoding="utf-8")))
                     for name in sorted(q.name for q in HOST_PKG.glob("*.py")))
        # The updater also passes its release root to the manifest verifier.
        # Keep these two indirect anchors explicit so a new one still fails.
        updater = (HOST_PKG / "self_update.py").read_text(encoding="utf-8")
        approved = [
            "Path(package or __file__).resolve().parent.parent.parent",
            "root=Path(__file__).resolve().parent.parent.parent, run=run, python=python)",
        ]
        for expression in approved:
            self.assertEqual(updater.count(expression), 1)
        # Windows PATH repair walks up from the venv interpreter, not from the
        # release tree, so it reads no release-relative file.
        venv_anchor = 'Path(sys.executable).resolve().parent.parent.parent / "bin"'
        native = (HOST_PKG / "native_sessions.py").read_text(encoding="utf-8")
        self.assertEqual(native.count(venv_anchor), 1)
        self.assertEqual(anchor_uses, direct + len(approved) + 1)
        self.assertTrue({"scripts/build-host-release.py", "scripts/verify-host-release.py",
                         "scripts/switch-host-release.py"}.issubset(manifest["files"]))
        for name in found:
            self.assertTrue(
                name in manifest["files"] or any(
                    name == tree or name.startswith(tree + "/") for tree in trees),
                "release-relative read missing from manifest: " + name,
            )

    def test_release_reads_are_the_known_small_set(self):
        # A new release-relative read must be a conscious manifest addition.
        found, _ = release_relative_paths()
        self.assertEqual(found, {
            "config/model-routing.json",
            "scripts/metrics-dashboard-export.py",
        })

    def test_verify_refuses_incomplete_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "rel"
            (target / "host" / "agent_hub_host").mkdir(parents=True)
            (target / "host" / "agent_hub_host" / "__init__.py").write_text("")
            (target / "host" / "agent_hub_host" / "fleet.py").write_text("#")
            (target / "config").mkdir()
            (target / "config" / "model-routing.json").write_text("{}")
            (target / "scripts").mkdir()
            (target / "scripts" / "session-host-supervisor.py").write_text("#")
            (target / "scripts" / "metrics-dashboard-export.py").write_text("#")
            (target / "pyproject.toml").write_text("")
            (target / "uv.lock").write_text("")
            (target / "release-manifest.json").write_text(json.dumps({
                "manifest_version": json.loads(MANIFEST.read_text())["version"],
                "release_rev": "0" * 40,
                "files_sha256": {"config/model-routing.json": "dead"},
            }))
            proc = subprocess.run([sys.executable, str(VERIFY), str(target)],
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)

    def test_verify_refuses_tampered_package_tree(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "rel"
            (target / "host" / "agent_hub_host").mkdir(parents=True)
            init = target / "host" / "agent_hub_host" / "__init__.py"
            init.write_text("# pristine")
            files = {}
            for name, body in [
                ("config/model-routing.json", "{}"),
                ("scripts/session-host-supervisor.py", "#"),
                ("scripts/metrics-dashboard-export.py", "#"),
                ("scripts/build-host-release.py", "#"),
                ("scripts/verify-host-release.py", "#"),
                ("scripts/switch-host-release.py", "#"),
                ("pyproject.toml", ""), ("uv.lock", ""),
            ]:
                dest = target / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(body)
                files[name] = hashlib.sha256(body.encode()).hexdigest()
            files["host/agent_hub_host/__init__.py"] = hashlib.sha256(b"# pristine").hexdigest()
            (target / "release-manifest.json").write_text(json.dumps({
                "manifest_version": json.loads(MANIFEST.read_text())["version"],
                "release_rev": "0" * 40,
                "files_sha256": files,
            }))
            init.write_text("# tampered")
            proc = subprocess.run([sys.executable, str(VERIFY), str(target)],
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("package file changed after build", proc.stdout)


class AliasRouteTests(unittest.TestCase):
    def test_alias_map_has_shared_go(self):
        aliases = account_aliases()
        self.assertIn("urgent-muse", aliases.get("shared-go", []))
        self.assertEqual(canonical_account_label("urgent-muse"), "shared-go")
        self.assertEqual(canonical_account_label("shared-go"), "shared-go")
        self.assertEqual(canonical_account_label("unrelated"), "unrelated")
        # Colliding entries fail closed: one alias, one canonical home.
        seen = {}
        for canonical, aliases_list in aliases.items():
            self.assertNotIn(canonical, seen)
            seen[canonical] = canonical
            for alias in aliases_list:
                self.assertNotIn(alias, seen)
                seen[alias] = canonical

    def test_route_matches_legacy_offer_against_canonical_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher = fleet.Dispatcher(go_settings(Path(tmp), "shared-go"))
            offer = {"id": "12345678-1234-1234-1234-123456789abc", "engine": "opencode",
                     "profile": None, "account": "urgent-muse", "mode": "headless",
                     "workspace": "demo"}
            self.assertEqual(dispatcher._route(offer)["account_label"], "shared-go")

    def test_route_matches_canonical_offer_against_legacy_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher = fleet.Dispatcher(go_settings(Path(tmp), "urgent-muse"))
            offer = {"id": "12345678-1234-1234-1234-123456789abc", "engine": "opencode",
                     "profile": None, "account": "shared-go", "mode": "headless",
                     "workspace": "demo"}
            self.assertEqual(dispatcher._route(offer)["account_label"], "urgent-muse")

    def test_route_still_refuses_unknown_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher = fleet.Dispatcher(go_settings(Path(tmp), "urgent-muse"))
            offer = {"id": "12345678-1234-1234-1234-123456789abc", "engine": "opencode",
                     "profile": None, "account": "nope", "mode": "headless",
                     "workspace": "demo"}
            with self.assertRaisesRegex(ValueError, "Local account/profile/workspace is not configured"):
                dispatcher._route(offer)

    def test_load_rejects_both_sides_of_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = go_settings(Path(tmp), "urgent-muse")
            cfg["subscriptions"].append(
                {"service": "go", "engine": "opencode", "account_label": "shared-go",
                 "profile": None, "admission_argv": ["maintained-gate"]})
            path = Path(tmp) / "subscriptions.json"
            path.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, "alias canonicalization"):
                fleet.load_settings(path)


class CapabilitiesPutTests(unittest.TestCase):
    def test_put_skipped_without_explicit_max_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = go_settings(Path(tmp), "urgent-muse")
            del cfg["max_agents"]
            calls, request, runner = fake_poll()
            with patch("agent_hub_host.system.get_system_info",
                       return_value={"capabilities": {}, "hostname": "t", "os": "t", "network": "home", "roles": []}), \
                 patch("agent_hub_host.api.get_heartbeat_payload", return_value={"type": "heartbeat"}):
                fleet.Dispatcher(cfg, request, runner).once()
            puts = [c for c in calls if c[0] == "PUT" and c[1] == "/api/fleet/routing/capabilities"]
            self.assertEqual(puts, [])

    def test_put_keeps_explicit_max_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = go_settings(Path(tmp), "urgent-muse")
            calls, request, runner = fake_poll()
            with patch("agent_hub_host.system.get_system_info",
                       return_value={"capabilities": {}, "hostname": "t", "os": "t", "network": "home", "roles": []}), \
                 patch("agent_hub_host.api.get_heartbeat_payload", return_value={"type": "heartbeat"}):
                fleet.Dispatcher(cfg, request, runner).once()
            puts = [c for c in calls if c[0] == "PUT" and c[1] == "/api/fleet/routing/capabilities"]
            self.assertEqual(len(puts), 1)
            self.assertEqual(puts[0][2]["max_agents"], 100)


class CrossAliasDispatchTests(unittest.TestCase):
    """Full dispatch() across a relabel boundary in both directions.

    The admission gate always sees the local subscription identity while the
    server claim carries the offered (registry route) account; canonical
    identity was already pinned by _route().
    """

    def run_cross_alias(self, host_label, offer_account):
        import datetime as dt
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir(mode=0o700)
            os.chmod(tmp, 0o700)
            cfg = go_settings(Path(tmp), host_label)
            cfg["workspaces"] = {"demo": str(workspace)}
            cfg["git"] = {"user.name": "T", "user.email": "t@example"}
            offer_id = "12345678-1234-1234-1234-123456789abc"
            seen = {}

            def fake_request(method, path, body=None):
                seen[(method, path)] = body
                return {}

            def fake_runner(argv, cwd=None, timeout=None, payload=None):
                if payload is not None:
                    seen["admission_payload"] = payload
                    return {
                        "launch_allowed": True, "profile_supported": True,
                        "account_label": host_label,
                        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                if len(argv) > 1 and argv[1] == "message":
                    return {}
                if len(argv) > 1 and argv[1] == "start":
                    return {"id": "native-1", "workspace": str(workspace),
                            "tag": "hub-" + offer_id, "engine": "opencode",
                            "profile": None, "phase": "running"}
                raise AssertionError("unexpected runner argv: " + " ".join(argv))

            offer = {"id": offer_id, "engine": "opencode", "profile": None,
                     "account": offer_account, "mode": "interactive",
                     "workspace": "demo"}
            # Route identity tests must not depend on shared-host load.
            with patch("agent_hub_host.host_load.check_live_load", return_value={}), \
                 patch.object(fleet.Dispatcher, "_lock_session_worktree"):
                result = fleet.Dispatcher(cfg, fake_request, fake_runner).dispatch(offer)
            self.assertEqual(result.get("state"), "running")
            self.assertEqual(result.get("native_id"), "native-1")
            # Gate sees the local subscription identity ...
            self.assertEqual(seen["admission_payload"]["account"], host_label)
            # ... while the server claim carries the offered route account.
            self.assertEqual(seen[("POST", "/api/fleet/launches/" + offer_id + "/claim")]["account"], offer_account)
            return result

    def test_canonical_host_serves_legacy_offer(self):
        self.run_cross_alias("shared-go", "urgent-muse")

    def test_legacy_host_serves_canonical_offer(self):
        self.run_cross_alias("urgent-muse", "shared-go")


class SwitchScriptTests(unittest.TestCase):
    def load_switch(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "switch_host_release", str(REPO / "scripts" / "switch-host-release.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_refuses_unverified_release_without_writing_dropin(self):
        module = self.load_switch()
        with tempfile.TemporaryDirectory() as tmp:
            dropin = Path(tmp) / "zzz-test.conf"
            with self.assertRaises(SystemExit):
                module.substitute_release("no release path here", "/new/dir")
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "switch-host-release.py"),
                 "--release", str(Path(tmp) / "missing"),
                 "--template", str(REPO / "scripts" / "switch-host-release.py"),
                 "--dropin", str(dropin)],
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(dropin.exists())

    def test_rewrites_dropin_without_doubling_prefix(self):
        module = self.load_switch()
        old = "/home/alexey/.local/share/agent-hub/host-releases/" + "6b129ed18cd8e7b71f27dbd161b8e5ebae1d1940"
        template = ("[Service]\n"
                    "WorkingDirectory=" + old + "\n"
                    "Environment=PYTHONPATH=" + old + "/host\n"
                    "Environment=TMPDIR=" + old + "/.local/tmp\n"
                    "ExecStart=\n"
                    "ExecStart=" + old + "/.venv/bin/python " + old + "/scripts/session-host-supervisor.py --workspace " + old + " --config /c.json\n")
        new = "/home/alexey/.local/share/agent-hub/host-releases/" + "a" * 40
        out = module.substitute_release(template, new)
        self.assertNotIn(old, out)
        self.assertNotIn("host-releases/home", out)
        self.assertIn("WorkingDirectory=" + new + "\n", out)
        self.assertIn("PYTHONPATH=" + new + "/host", out)
        self.assertIn("--workspace " + new + " --config", out)


class CheckRoutesTests(unittest.TestCase):
    def test_zero_matching_routes_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = Path(tmp) / "subscriptions.json"
            settings_file.write_text(json.dumps({
                "version": 1, "workspaces": {"demo": str(tmp)},
                "capabilities": [], "max_agents": 100,
                "subscriptions": [
                    {"service": "go", "engine": "opencode",
                     "account_label": "urgent-muse", "profile": None,
                     "admission_argv": ["maintained-gate"]},
                ],
            }))
            fleet_snapshot = Path(tmp) / "fleet.json"
            fleet_snapshot.write_text(json.dumps({"routes": []}))
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "check-host-routes.py"),
                 "--settings", str(settings_file),
                 "--fleet", str(fleet_snapshot), "--machine", "NO-SUCH-MACHINE"],
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("no registry routes", (proc.stdout or "") + (proc.stderr or ""))


if __name__ == "__main__":
    unittest.main()
