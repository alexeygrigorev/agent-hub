"""AC1/AC2/AC3: canonical account catalog, host render and drift check.

Reads the repo config/model-routing.json catalog (vendored into releases by
scripts/build-host-release.py), so these tests guard the exact document both
runtimes consume.
"""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import account_registry

REPO = Path(__file__).resolve().parents[2]
POLICY = json.loads((REPO / "config" / "model-routing.json").read_text())


def secure_dir(path):
    """private_fs refuses group/other bits; make tmp trees compliant."""
    target = Path(path)
    target.chmod(0o700)
    for child in target.rglob("*"):
        if child.is_dir() and not child.is_symlink():
            child.chmod(0o700)
        elif child.is_file() and not child.is_symlink():
            child.chmod(0o600)
    return target


def sample_accounts():
    return copy.deepcopy(POLICY["accounts"])


def settings_with(subs):
    return {"version": 1, "subscriptions": subs, "workspaces": {"home": "/home/test"}}


class CatalogTest(unittest.TestCase):
    def test_loads_seven_canonical_accounts(self):
        accounts = account_registry.policy_accounts(POLICY)
        self.assertEqual(7, len(accounts))
        by_account = {a["account"]: a for a in accounts}
        self.assertEqual("codex", by_account["shared-codex"]["harness"])
        self.assertEqual("hub-shared-codex", by_account["shared-codex"]["aplexer_profile"])
        self.assertEqual({"mode": "managed-bundle", "bundle": "shared-codex"},
                         by_account["shared-codex"]["credential"])
        self.assertEqual(["quse", "codex", "--json"], by_account["shared-codex"]["quota_probe"])
        self.assertIsNone(by_account["shared-zai"]["aplexer_profile"])
        self.assertEqual({"mode": "host-home", "path": "~/.zcode"},
                         by_account["shared-zai"]["credential"])

    def test_rejects_duplicate_model_ids_via_policy(self):
        ids = [m["id"] for m in POLICY["models"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rejects_bad_catalog(self):
        for mutate in (lambda a: a.__setitem__("account", "codex-orin"),
                       lambda a: a["credential"].__setitem__("mode", "vault"),
                       lambda a: a.__setitem__("quota_probe", "not-a-list"),
                       lambda a: a.__setitem__("aplexer_profile", "codex")):
            bad = sample_accounts()
            mutate(bad[0])
            with self.assertRaises(ValueError):
                account_registry.policy_accounts({"accounts": bad})
        with self.assertRaises(ValueError):
            account_registry.policy_accounts({"accounts": []})


class RenderTest(unittest.TestCase):
    def test_merge_sets_owned_fields_and_preserves_host_local(self):
        accounts = sample_accounts()
        settings = settings_with([{
            "account_label": "codex-orin", "service": "codex", "engine": "codex",
            "profile": "hub-codex-orin",
            "quota_argv": ["quse", "codex", "--profile", "hub-codex-orin", "--json"],
            "admission_argv": ["local", "probe"],
        }])
        entries, changes = account_registry.diff_subscriptions(settings, accounts)
        orin = next(s for s in entries if s["account_label"] == "codex-orin")
        self.assertEqual(["quse", "codex-orin", "--json"], orin["quota_argv"])
        self.assertEqual(["local", "probe"], orin["admission_argv"])
        self.assertTrue(any("codex-orin" in c and "quota_argv" in c for c in changes))
        # Alias label is kept, never force-renamed by sync.
        legacy = settings_with([{"account_label": "urgent-muse", "service": "go"}])
        entries, _ = account_registry.diff_subscriptions(legacy, accounts)
        labels = [s["account_label"] for s in entries]
        self.assertIn("urgent-muse", labels)
        self.assertNotIn("shared-go", labels)
        muse = next(s for s in entries if s["account_label"] == "urgent-muse")
        self.assertEqual("opencode", muse["engine"])
        self.assertEqual(["quse", "go", "--json"], muse["quota_argv"])

    def test_adds_missing_accounts_and_keeps_unknown(self):
        accounts = sample_accounts()
        entries, changes = account_registry.diff_subscriptions(
            settings_with([{"account_label": "custom", "service": "go"}]), accounts)
        self.assertEqual(8, len(entries))
        self.assertTrue(any(s["account_label"] == "shared-codex" for s in entries))
        self.assertTrue(any(s["account_label"] == "custom" for s in entries))
        self.assertTrue(any("add entry" in c for c in changes))

    def test_rejects_ambiguous_canonical_routes(self):
        accounts = sample_accounts()
        settings = settings_with([
            {"account_label": "urgent-muse", "service": "go", "engine": "opencode", "profile": None},
            {"account_label": "shared-go", "service": "go", "engine": "opencode", "profile": None},
        ])
        with self.assertRaises(ValueError):
            account_registry.diff_subscriptions(settings, accounts)

    def test_sync_settings_dry_run_and_write(self):
        import tempfile
        accounts = sample_accounts()
        with tempfile.TemporaryDirectory() as tmp:
            secure_dir(tmp)
            path = Path(tmp) / "subscriptions.json"
            data = settings_with([{"account_label": "shared-codex", "service": "codex",
                                    "engine": "codex", "profile": "hub-shared-codex",
                                    "quota_argv": ["quse", "codex", "--json"]}])
            path.write_text(json.dumps(data))
            path.chmod(0o600)
            dry = account_registry.sync_settings(str(path), accounts, write=False)
            self.assertTrue(dry["changes"])
            self.assertFalse(dry["wrote"])
            self.assertEqual(json.loads(path.read_text())["subscriptions"], data["subscriptions"])
            done = account_registry.sync_settings(str(path), accounts, write=True)
            self.assertTrue(done["wrote"])
            written = json.loads(path.read_text())
            self.assertEqual(7, len(written["subscriptions"]))
            again = account_registry.sync_settings(str(path), accounts, write=True)
            self.assertFalse(again["changes"] or again["wrote"])


class AplexerTest(unittest.TestCase):
    def _env(self, tmp, state=None, config_text="version = 1\n"):
        secure_dir(tmp)
        managed = Path(tmp) / "managed"
        managed.mkdir(mode=0o700)
        if state is not None:
            state_file = managed / "state.json"
            state_file.write_text(json.dumps(state))
            state_file.chmod(0o600)
        cfg = Path(tmp) / "config.toml"
        cfg.write_text(config_text)
        cfg.chmod(0o600)
        return managed, cfg

    def test_renders_installed_bundle_profile(self):
        import tempfile
        accounts = sample_accounts()
        with tempfile.TemporaryDirectory() as tmp:
            managed, cfg = self._env(tmp, [{"label": "codex-orin", "revision": "r", "provider": "codex"}])
            env = {"AGENT_HUB_MANAGED_DIR": str(managed), "APLEXER_CONFIG": str(cfg)}
            with patch.dict(os.environ, env):
                res = account_registry.sync_aplexer_profiles(None, accounts, write=False)
                self.assertIn("hub-codex-orin", res["managed"])
                self.assertTrue(res["changes"])
                self.assertFalse(res["wrote"])
                res = account_registry.sync_aplexer_profiles(None, accounts, write=True)
                self.assertTrue(res["wrote"])
                text = cfg.read_text()
                self.assertIn("[profiles.hub-codex-orin]", text)
                self.assertIn('engine = "codex"', text)
                again = account_registry.sync_aplexer_profiles(None, accounts, write=True)
                self.assertFalse(again["changes"] or again["wrote"])

    def test_no_bundles_no_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            managed, cfg = self._env(tmp, [])
            env = {"AGENT_HUB_MANAGED_DIR": str(managed), "APLEXER_CONFIG": str(cfg)}
            with patch.dict(os.environ, env):
                res = account_registry.sync_aplexer_profiles(None, sample_accounts(), write=True)
                self.assertFalse(res["changes"] or res["wrote"])
                self.assertNotIn("BEGIN HUB MANAGED", cfg.read_text())


class ManagedSubscriptionTest(unittest.TestCase):
    def test_managed_entries_carry_no_host_quota_probe(self):
        """Intake 5814e109 I4: Hub reads quota; managed subscriptions carry no quse argv."""
        import tempfile
        from agent_hub_host import provisioning
        with tempfile.TemporaryDirectory() as tmp:
            managed = Path(tmp) / "managed"
            managed.mkdir(mode=0o700)
            (managed / "state.json").write_text(json.dumps(
                [{"label": "codex-orin", "revision": "r", "provider": "codex"}]))
            (managed / "state.json").chmod(0o600)
            with patch.dict(os.environ, {"AGENT_HUB_MANAGED_DIR": str(managed)}):
                subs = provisioning.managed_subscriptions()
            orin = next(s for s in subs if s["account_label"] == "codex-orin")
            self.assertNotIn("quota_argv", orin)
            self.assertEqual("hub-codex-orin", orin["profile"])


class DriftTest(unittest.TestCase):
    def _clean(self, accounts):
        subs = [{"account_label": ("urgent-muse" if a["account"] == "shared-go" else a["account"]),
                 "service": a["provider"], "engine": a["harness"], "profile": a["aplexer_profile"],
                 "quota_argv": list(a["quota_probe"])} for a in accounts]
        routes = [{"machine_id": "m1", "engine": a["harness"], "account": a["account"], "profile": a["aplexer_profile"]}
                  for a in accounts]
        aplexer = ("version = 1\n\n# BEGIN HUB MANAGED ENGINES\n\n"
                   "[profiles.hub-shared-codex]\nengine = \"codex\"\ncommand = [\"x\", \"codex\"]\n"
                   "[profiles.hub-codex-orin]\nengine = \"codex\"\ncommand = [\"x\", \"codex-orin\"]\n"
                   "# END HUB MANAGED ENGINES\n")
        return subs, routes, aplexer

    def test_clean_when_alias_tolerant(self):
        import tempfile
        accounts = sample_accounts()
        subs, routes, aplexer = self._clean(accounts)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for a in accounts:
                if a["credential"]["mode"] == "host-home":
                    (home / a["credential"]["path"][2:]).mkdir(parents=True)
            managed = Path(tmp) / "managed"
            managed.mkdir()
            for a in accounts:
                if a["credential"]["mode"] == "managed-bundle":
                    (managed / (a["credential"]["bundle"] + "-rev")).mkdir()
            probes = {a["account"]: (True, "ok") for a in accounts}
            problems = account_registry.check_drift(accounts, subs, aplexer, routes,
                                                    probe_results=probes, home=str(home),
                                                    managed_root=managed)
            self.assertEqual([], problems)

    def test_flags_each_source(self):
        accounts = sample_accounts()
        subs, routes, aplexer = self._clean(accounts)
        subs = [s for s in subs if s["account_label"] != "codex-orin"]
        orin_route = next(r for r in routes if r["account"] == "codex-orin")
        orin_route["engine"] = "bogus"
        aplexer = aplexer.replace("[profiles.hub-codex-orin]", "[profiles.hub-stray]")
        probes = {"shared-zai": (False, "exit 2: No such option")}
        problems = account_registry.check_drift(accounts, subs, aplexer, routes,
                                                probe_results=probes, home="/nonexistent-home-xyz",
                                                managed_root=Path("/nonexistent-managed-xyz"))
        joined = "\n".join(problems)
        for needle in ("subscriptions: account codex-orin has no host entry",
                       "routes: bogus/codex-orin",
                       "aplexer: managed profile [hub-codex-orin] is missing",
                       "quota: shared-zai exit 2: No such option",
                       "credential: home path"):
            self.assertIn(needle, joined)


class MachineTest(unittest.TestCase):
    MACHINES = [
        {"id": "new-id", "name": "hetzner", "hostname": "RMTHZ", "alias_of": None, "canonical_id": "new-id"},
        {"id": "old-id", "name": "old", "hostname": "RMTHZ", "alias_of": "new-id", "canonical_id": "new-id"},
    ]

    def test_resolves_by_hostname_through_alias(self):
        self.assertEqual("new-id", account_registry.resolve_machine(self.MACHINES, hostname="RMTHZ"))

    def test_explicit_wins(self):
        self.assertEqual("new-id", account_registry.resolve_machine(self.MACHINES, hostname="X", explicit="old-id"))

    def test_unknown_returns_none(self):
        self.assertIsNone(account_registry.resolve_machine(self.MACHINES, hostname="nowhere"))


if __name__ == "__main__":
    unittest.main()
