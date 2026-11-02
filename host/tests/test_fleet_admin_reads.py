import contextlib
import io
import sys
import unittest
from unittest.mock import patch


class FleetAdminReadCliTest(unittest.TestCase):
    def run_cli(self, argv):
        from agent_hub_host import cli, session_control

        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {"rows": []}

        with patch.object(sys, "argv", ["hub", *argv]), \
                patch.object(session_control, "request", side_effect=request), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        return calls

    def test_reserved_launches_uses_admin_api_and_bounded_page(self):
        calls = self.run_cli(["fleet-reservations", "hub claude", "--limit", "100", "--cursor", "next/page"])
        self.assertEqual(calls, [("GET", "/api/fleet/accounts/hub%20claude/reserved-launches?limit=100&cursor=next%2Fpage", None)])

    def test_capacity_history_passes_account_window_and_limit(self):
        calls = self.run_cli([
            "fleet-capacity-history", "hub-claude",
            "--from", "2026-11-01T04:20:00Z", "--to", "2026-11-01T05:00:00Z",
            "--cursor", "1793507000000",
        ])
        self.assertEqual(calls, [("GET", "/api/fleet/accounts/hub-claude/capacity-history?from=2026-11-01T04%3A20%3A00Z&to=2026-11-01T05%3A00%3A00Z&limit=100&cursor=1793507000000", None)])

    def test_reserved_launches_rejects_unbounded_page_before_network(self):
        from agent_hub_host import cli, session_control

        with patch.object(sys, "argv", ["hub", "fleet-reservations", "hub-claude", "--limit", "101"]), \
                patch.object(session_control, "request") as request, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 2)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
