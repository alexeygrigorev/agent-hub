"""Intake 608a2f9a: hosts advertise catalog host-home accounts on their own.

A host renders shared-zai/shared-gemini/shared-go only when the engine CLI
and the credential home exist locally; operator entries always win and
disabled providers (grok) and claude are never auto-rendered.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import account_registry, fleet

REPO = Path(__file__).resolve().parents[2]
ACCOUNTS = json.loads((REPO / "config" / "model-routing.json").read_text())["accounts"]


class NativeSubscriptionsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="native-subs-")
        self.bins = {}

    def which(self, name):
        return self.bins.get(name)

    def login(self, *parts):
        Path(self.home, *parts).mkdir(parents=True, exist_ok=True)

    def render(self, subs=()):
        return account_registry.native_subscriptions(
            {"subscriptions": list(subs)}, ACCOUNTS, which=self.which, home=self.home, python="/py")

    def test_nothing_without_cli_or_login(self):
        self.assertEqual(self.render(), [])
        self.bins = {"zcodex": "/bin/zcodex", "agy": "/bin/agy", "opencode": "/bin/opencode"}
        self.assertEqual(self.render(), [])

    def test_renders_installed_logged_in_accounts(self):
        self.bins = {"zcodex": "/bin/zcodex", "agy": "/bin/agy", "opencode": "/bin/opencode",
                     "grok": "/bin/grok", "claude": "/bin/claude"}
        for path in (".zcode", ".gemini", ".grok", ".claude", ".local/share/opencode"):
            self.login(*path.split("/"))
        Path(self.home, ".local/share/opencode/auth.json").write_text("{}")
        subs = {s["account_label"]: s for s in self.render()}
        self.assertEqual(sorted(subs), ["shared-gemini", "shared-go", "shared-zai"])
        zai = subs["shared-zai"]
        self.assertEqual((zai["service"], zai["engine"], zai["profile"]), ("zai", "zcodex", None))
        self.assertEqual(zai["admission_argv"], ["/py", "-m", "agent_hub_host.native_admission", "--service", "zai",
                                                 "--account", "shared-zai", "--engine", "zcodex"])
        self.assertEqual(zai["startup_argv"][0], "/bin/zcodex")
        self.assertEqual((zai["launcher_provider"], zai["launcher_model"]), ("zai", "glm-5.3-flash"))
        self.assertEqual((subs["shared-gemini"]["engine"], subs["shared-gemini"]["launcher_provider"]), ("antigravity", "antigravity"))
        self.assertEqual(subs["shared-go"]["launcher_model"], "opencode-go/muse-spark-1.3-contributor")
        self.assertNotIn("startup_argv", subs["shared-go"])
        # Every rendered entry passes the host's own settings validation.
        path = Path(self.home, "subscriptions.json")
        path.write_text(json.dumps({"version": 1, "subscriptions": list(subs.values()), "workspaces": {"home": self.home}}))
        self.assertEqual(len(fleet.load_settings(str(path))["subscriptions"]), 3)

    def test_opencode_needs_auth_file(self):
        self.bins = {"opencode": "/bin/opencode"}
        self.login(".local", "share", "opencode")
        self.assertEqual(self.render(), [])

    def test_operator_entry_wins_including_legacy_label(self):
        self.bins = {"zcodex": "/bin/zcodex"}
        self.login(".zcode")
        self.assertEqual(self.render([{"account_label": "shared-zai", "service": "zai"}]), [])

    def test_local_bin_fallback(self):
        self.login(".zcode")
        local = Path(self.home, ".local", "bin")
        local.mkdir(parents=True)
        (local / "zcodex").write_text("#!/bin/sh\n")
        (local / "zcodex").chmod(0o700)
        subs = self.render()
        self.assertEqual([s["account_label"] for s in subs], ["shared-zai"])
        self.assertEqual(subs[0]["startup_argv"][0], str(local / "zcodex"))


class DispatcherMergeTest(unittest.TestCase):
    def dispatcher(self, subs):
        d = fleet.Dispatcher.__new__(fleet.Dispatcher)
        d.settings = {"subscriptions": subs}
        return d

    def test_merge_adds_and_retracts(self):
        operator = {"account_label": "codex-orin", "service": "codex"}
        d = self.dispatcher([operator])
        native = {"account_label": "shared-zai", "service": "zai", "engine": "zcodex"}
        with patch.object(account_registry, "native_subscriptions", return_value=[native]):
            d._merge_native_subscriptions()
        self.assertEqual(d.settings["subscriptions"], [operator, native])
        with patch.object(account_registry, "native_subscriptions", return_value=[]) as render:
            d._merge_native_subscriptions()
        # The previous native entry is not treated as an operator entry.
        self.assertEqual(render.call_args[0][0], {"subscriptions": [operator]})
        self.assertEqual(d.settings["subscriptions"], [operator])

    def test_render_failure_keeps_last_native_set(self):
        native = {"account_label": "shared-zai", "service": "zai", "engine": "zcodex"}
        d = self.dispatcher([])
        with patch.object(account_registry, "native_subscriptions", return_value=[native]):
            d._merge_native_subscriptions()
        with patch.object(account_registry, "native_subscriptions", side_effect=ValueError("bad catalog")):
            d._merge_native_subscriptions()
        self.assertEqual(d.settings["subscriptions"], [native])


if __name__ == "__main__":
    unittest.main()
