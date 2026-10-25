import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet


class FleetSubscriptionsAdvertisingTest(unittest.TestCase):
    def test_canonical_config_and_managed_accounts_merge_without_losing_native_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'subscriptions.json'
            native = [
                {'service': 'claude', 'engine': 'claude', 'account_label': 'shared-claude', 'admission_argv': ['native-claude-gate']},
                {'service': 'zai', 'engine': 'zcodex', 'account_label': 'shared-zai', 'admission_argv': ['native-zai-gate']},
                {'service': 'grok', 'engine': 'grok', 'account_label': 'shared-grok'},
            ]
            path.write_text(json.dumps({'version': 1, 'workspaces': {'demo': tmp}, 'subscriptions': native + [
                {'service': 'codex', 'engine': 'codex', 'account_label': 'shared-codex'},
            ]}))
            managed = [{'service': 'codex', 'engine': 'codex', 'account_label': 'shared-codex', 'profile': 'hub-shared-codex', 'admission_argv': ['managed-gate']}]
            with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=managed):
                loaded = fleet.load_settings(path)
            self.assertEqual(loaded['subscriptions'], native + managed)
            self.assertEqual(len({s['account_label'] for s in loaded['subscriptions']}), 4)

    def test_dispatcher_advertises_all_configured_subscription_engines(self):
        calls = []

        def request(method, path, body=None):
            calls.append((method, path, body))
            if path == "/api/host/poll":
                return {"offers": []}
            return {}

        def runner(argv):
            if argv[:2] == ["quse", "claude"]:
                return {
                    "claude": {
                        "status": "ok",
                        "windows": {
                            "5h": {
                                "percent_remaining": 95.0,
                                "reset_at": "2026-10-24T19:00:00Z",
                                "rolling": False,
                            },
                            "7d": {
                                "percent_remaining": 99.0,
                                "reset_at": "2026-10-29T15:00:00Z",
                            },
                        },
                    }
                }
            if argv[:1] == ["claude"] and argv[1:3] == ["auth", "status"]:
                return {"subscriptionType": "max"}
            if argv[:1] == ["aplexer"]:
                return []
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws_dir = tmp_path / "workspace"
            ws_dir.mkdir()

            cfg = {
                "machine_id": "test-machine-1",
                "aplexer_state_dir": tmp,
                "workspaces": {"main": str(ws_dir)},
                "subscriptions": [
                    {"service": "claude", "account_label": "shared-claude"},
                    {"service": "codex", "account_label": "shared-codex"},
                    {"service": "gemini", "account_label": "shared-gemini", "engine": "antigravity"},
                    {"service": "go", "account_label": "urgent-muse", "engine": "opencode"},
                    {"service": "grok", "account_label": "shared-grok"},
                    {"service": "zai", "account_label": "shared-zai", "engine": "zcodex"},
                ],
            }

            with patch("agent_hub_host.system.get_system_info", return_value={"capabilities": {}, "hostname": "hetzner", "os": "linux", "network": "home", "roles": []}), \
                 patch("agent_hub_host.api.get_heartbeat_payload", return_value={"type": "heartbeat"}):
                dispatcher = fleet.Dispatcher(cfg, request, runner)
                dispatcher.once()

            register_calls = [c for c in calls if c[0] == "POST" and c[1] == "/api/machines/register"]
            self.assertEqual(len(register_calls), 1)
            registered_engines = register_calls[0][2]["capabilities"]["engines"]
            expected_engines = sorted(["claude", "codex", "antigravity", "opencode", "grok", "zcodex"])
            self.assertEqual(registered_engines, expected_engines)

            sub_calls = [c for c in calls if c[0] == "PUT" and c[1] == "/api/fleet/subscriptions"]
            self.assertEqual(len(sub_calls), 1)
            reported_subs = sub_calls[0][2]["subscriptions"]
            reported_accounts = [s["account"] for s in reported_subs]
            self.assertIn("shared-claude", reported_accounts)
            self.assertIn("shared-codex", reported_accounts)
            self.assertIn("shared-gemini", reported_accounts)
            self.assertIn("urgent-muse", reported_accounts)
            self.assertIn("shared-grok", reported_accounts)
            self.assertIn("shared-zai", reported_accounts)

    def test_monitor_only_account_does_not_disable_runnable_account_on_same_engine(self):
        from unittest.mock import Mock
        calls = []
        cfg = {'workspaces': {}, 'subscriptions': [
            {'service': 'zai', 'engine': 'codex', 'account_label': 'monitor-only'},
            {'service': 'codex', 'engine': 'codex', 'account_label': 'healthy', 'admission_argv': ['gate']},
            {'service': 'zai', 'engine': 'zcodex', 'account_label': 'broken'},
        ]}
        dispatcher = fleet.Dispatcher(cfg, lambda m, p, b=None: calls.append((m, p, b)) or {}, Mock(return_value={}))
        with patch('agent_hub_host.provisioning.tick'), \
             patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]), \
             patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}), \
             patch('agent_hub_host.api.get_heartbeat_payload', return_value={}), \
             patch('agent_hub_host.fleet.collect_subscriptions', return_value=[]), \
             patch.object(dispatcher, 'report_catalog_bindings', return_value=[]), \
             patch.object(dispatcher, 'reconcile_launches', return_value=[]), \
             patch.object(dispatcher, 'reconcile_imported_sessions', return_value=[]):
            dispatcher.once()
        self.assertEqual([b for _, p, b in calls if p == '/api/fleet/probe'], [
            {'engine': 'zcodex', 'status': 'unavailable', 'reason': 'admission_unconfigured'}])

    def test_collect_subscriptions_claude(self):
        def runner(argv):
            if argv[:2] == ["quse", "claude"]:
                return {
                    "claude": {
                        "status": "ok",
                        "windows": {
                            "5h": {
                                "percent_remaining": 80.0,
                                "reset_at": "2026-10-24T19:00:00Z",
                                "rolling": False,
                            },
                            "7d": {
                                "percent_remaining": 90.0,
                                "reset_at": "2026-10-29T15:00:00Z",
                            },
                        },
                        "details": {"subscription": "max"},
                    }
                }
            if argv[:1] == ["claude"]:
                return {"subscriptionType": "max"}
            return {}

        settings = {
            "subscriptions": [
                {
                    "service": "claude",
                    "account_label": "shared-claude",
                    "quota_argv": ["quse", "claude", "--json"],
                }
            ]
        }
        reports = fleet.collect_subscriptions(settings, runner=runner)
        self.assertEqual(len(reports), 1)
        sub = reports[0]
        self.assertEqual(sub["service"], "claude")
        self.assertEqual(sub["account"], "shared-claude")
        self.assertEqual(sub["plan"], "max")
        self.assertIsNotNone(sub["quota"])
        self.assertEqual(sub["quota"]["status"], "ok")
        self.assertEqual(sub["quota"]["windows"]["5h"]["percent_remaining"], 80.0)

    def test_supervisor_default_config_path(self):
        supervisor_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "session-host-supervisor.py"
        content = supervisor_path.read_text(encoding="utf-8")
        self.assertIn("config_path = fleet.subscriptions_path().resolve(strict=True)", content)


if __name__ == "__main__":
    unittest.main()
