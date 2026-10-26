"""TBM AC4 CLI: pacing / banked / kill-switch dispatch and rendering.

Offline only: session_control.request is stubbed, no subprocess, no network,
no real credentials.
"""
import argparse
import unittest

from agent_hub_host import cli as cli_mod
from agent_hub_host import quota as quota_cli
from agent_hub_host import session_control
from agent_hub_host import tbm as tbm_cli


def make_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    quota_cli.add_commands(sub)
    return parser


class TbmParserTests(unittest.TestCase):
    def test_existing_fleet_args_preserved(self):
        parser = make_parser()
        args = parser.parse_args(["quota"])
        self.assertIsNone(args.host)
        self.assertIsNone(args.provider)
        self.assertIsNone(args.engine)
        self.assertIsNone(args.account)
        self.assertIsNone(args.admin_token_file)
        self.assertFalse(args.json)
        self.assertIsNone(args.tbm)
        self.assertIsNone(args.kill_action)
        self.assertIsNone(args.reason)

    def test_fleet_filters_still_parse(self):
        parser = make_parser()
        args = parser.parse_args(
            ["quota", "--host", "m1", "--provider", "codex",
             "--engine", "codex", "--account", "main"])
        self.assertEqual(args.host, "m1")
        self.assertEqual(args.provider, "codex")
        self.assertIsNone(args.tbm)

    def test_tbm_views_parse(self):
        parser = make_parser()
        self.assertEqual(parser.parse_args(["quota", "pacing"]).tbm, "pacing")
        self.assertEqual(parser.parse_args(["quota", "banked"]).tbm, "banked")
        args = parser.parse_args(["quota", "kill-switch"])
        self.assertEqual(args.tbm, "kill-switch")
        self.assertIsNone(args.kill_action)
        args = parser.parse_args(["quota", "kill-switch", "on", "--reason", "test"])
        self.assertEqual(args.kill_action, "on")
        self.assertEqual(args.reason, "test")
        args = parser.parse_args(["quota", "pacing", "--json"])
        self.assertTrue(args.json)

    def test_kill_switch_choices_enforced(self):
        parser = make_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["quota", "kill-switch", "maybe"])


class TbmDispatchTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = {}
        self.real = session_control.request

        def fake(method, path, payload=None):
            self.calls.append((method, path, payload))
            key = (method, path)
            if key not in self.responses:
                raise AssertionError("unexpected request %r" % (key,))
            return self.responses[key]

        session_control.request = fake

    def tearDown(self):
        session_control.request = self.real

    def _fleet_args(self, **over):
        base = dict(host=None, provider=None, engine=None, account=None,
                     admin_token_file=None, json=False,
                     tbm=None, kill_action=None, reason=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_pacing_get(self):
        self.responses[("GET", "/api/quota/pacing")] = {"as_of": "2026-10-26T00:00:00Z", "pools": []}
        data = quota_cli.execute(self._fleet_args(tbm="pacing"))
        self.assertEqual(self.calls, [("GET", "/api/quota/pacing", None)])
        self.assertIn("pools", data)

    def test_banked_get(self):
        self.responses[("GET", "/api/quota/banked")] = {"as_of": "2026-10-26T00:00:00Z", "tranches": []}
        data = quota_cli.execute(self._fleet_args(tbm="banked"))
        self.assertEqual(self.calls, [("GET", "/api/quota/banked", None)])
        self.assertIn("tranches", data)

    def test_kill_switch_get(self):
        self.responses[("GET", "/api/quota/kill-switch")] = {"active": False, "reason": ""}
        data = quota_cli.execute(self._fleet_args(tbm="kill-switch"))
        self.assertEqual(self.calls, [("GET", "/api/quota/kill-switch", None)])
        self.assertEqual(data, {"active": False, "reason": ""})

    def test_kill_switch_on_posts_active_reason(self):
        self.responses[("POST", "/api/quota/kill-switch")] = {"active": True, "reason": "staged test"}
        data = quota_cli.execute(self._fleet_args(tbm="kill-switch", kill_action="on", reason="staged test"))
        self.assertEqual(self.calls,
                         [("POST", "/api/quota/kill-switch", {"active": True, "reason": "staged test"})])
        self.assertTrue(data["active"])

    def test_kill_switch_off_empty_reason(self):
        self.responses[("POST", "/api/quota/kill-switch")] = {"active": False, "reason": ""}
        data = quota_cli.execute(self._fleet_args(tbm="kill-switch", kill_action="off"))
        self.assertEqual(self.calls,
                         [("POST", "/api/quota/kill-switch", {"active": False, "reason": ""})])
        self.assertFalse(data["active"])

    def test_filters_rejected_with_tbm_view(self):
        with self.assertRaises(ValueError):
            quota_cli.execute(self._fleet_args(tbm="pacing", provider="codex"))
        with self.assertRaises(ValueError):
            quota_cli.execute(self._fleet_args(tbm="banked", host="m1"))

    def test_reason_rejected_on_read(self):
        with self.assertRaises(ValueError):
            quota_cli.execute(self._fleet_args(tbm="kill-switch", reason="x"))

    def test_reason_length_bounded(self):
        with self.assertRaises(ValueError):
            tbm_cli.set_kill_switch(True, "x" * 501)

    def test_non_dict_response_rejected(self):
        self.responses[("GET", "/api/quota/pacing")] = ["not", "a", "dict"]
        with self.assertRaises(ValueError):
            quota_cli.execute(self._fleet_args(tbm="pacing"))

    def test_fleet_path_unchanged(self):
        import agent_hub_host.bus as bus
        calls = []
        real = bus._do_request
        bus._do_request = lambda method, path: calls.append((method, path)) or {"pools": []}
        try:
            quota_cli.execute(self._fleet_args(host="m1", provider="codex"))
        finally:
            bus._do_request = real
        self.assertEqual(len(calls), 1)
        self.assertIn("view=fleet", calls[0][1])


class TbmRenderTests(unittest.TestCase):
    def test_render_pacing_unknown_safe(self):
        text = tbm_cli.render_text("pacing", {"as_of": "2026-10-26T00:00:00Z", "pools": [
            {"pool_id": "codex:main", "target_burn_rate_tpm": None,
             "pacing_ratio": None, "reset_at_ms": None}]})
        self.assertIn("codex:main", text)
        self.assertIn("unknown", text)
        self.assertNotIn("0%", text)

    def test_render_banked_empty(self):
        text = tbm_cli.render_text("banked", {"as_of": "2026-10-26T00:00:00Z", "tranches": []})
        self.assertIn("No banked tranches", text)

    def test_render_banked_row(self):
        text = tbm_cli.render_text("banked", {"tranches": [
            {"tranche_id": "t1", "status": "active",
             "remaining_tokens": 100, "expires_at_ms": 123}]})
        self.assertIn("t1", text)
        self.assertIn("100", text)

    def test_render_kill_switch(self):
        self.assertIn("ON", tbm_cli.render_text("kill-switch", {"active": True, "reason": "hold"}))
        self.assertIn("OFF", tbm_cli.render_text("kill-switch", {"active": False, "reason": ""}))

    def test_render_rejects_bad_payload(self):
        with self.assertRaises(ValueError):
            tbm_cli.render_text("pacing", ["nope"])
        with self.assertRaises(ValueError):
            tbm_cli.render_text("kill-switch", {"active": "yes"})

    def test_parse_kill_action(self):
        self.assertTrue(tbm_cli.parse_kill_action("on"))
        self.assertFalse(tbm_cli.parse_kill_action("off"))
        with self.assertRaises(ValueError):
            tbm_cli.parse_kill_action("maybe")


class TbmCliWiringTests(unittest.TestCase):
    def test_cli_quota_parser_has_tbm(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd", required=True)
        cli_mod_path = cli_mod  # import guard: cli module loads
        self.assertTrue(hasattr(cli_mod_path, "main"))
        quota_cli.add_commands(sub)
        args = parser.parse_args(["quota", "pacing", "--json"])
        self.assertEqual(args.tbm, "pacing")
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
