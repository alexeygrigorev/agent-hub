import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


class TestMachinesAvailable(unittest.TestCase):
    """Regression for the authenticated Hub2 routing-machines projection."""

    def run_cli(self, response, *extra):
        from agent_hub_host import cli, bus
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["hub", "machines", "available", *extra]), \
                patch.object(bus, "_do_request", return_value=response) as request, \
                redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
        request.assert_called_once_with("GET", "/api/fleet/routing/machines")
        return code, out.getvalue(), err.getvalue()

    def test_connected_machines_shape_renders_plain_ids(self):
        code, out, err = self.run_cli({"machines": [
            {"machine_id": "hetzner", "connected": True},
            {"machine_id": "desktop", "connected": False},
        ]})
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["hetzner"])
        self.assertNotIn("reachable: none", err)

    def test_connected_machines_shape_json(self):
        code, out, _ = self.run_cli({"machines": [
            {"machine_id": "hetzner", "connected": True, "os": "linux"},
            {"machine_id": "offline", "connected": False},
        ]}, "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["reachable"][0]["id"], "hetzner")
        self.assertEqual(result["reachable"][0]["machine_id"], "hetzner")

    def test_empty_machines_reports_none(self):
        code, out, err = self.run_cli({"machines": [{"machine_id": "offline", "connected": False}]})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("reachable: none", err)

    def test_unknown_shape_fails_loudly(self):
        code, _, err = self.run_cli({"unexpected": True})
        self.assertEqual(code, 1)
        self.assertIn("unexpected /api/fleet/routing/machines response shape", err)

    def test_human_auth_sessions_and_machines_share_the_user_profile(self):
        from agent_hub_host import api, cli, config, worker_auth

        requests = []

        def response_for(req, timeout=None, **kwargs):
            requests.append(req)
            if req.full_url.endswith("/api/auth/me"):
                return {"kind": "human", "email": "orin@datatalks.club"}
            if "/api/sessions?" in req.full_url:
                return {"sessions": [], "next_cursor": None}
            if req.full_url.endswith("/api/fleet/routing/machines"):
                return {"machines": [{"machine_id": "grok", "connected": True}]}
            raise AssertionError("unexpected Hub request path")

        def invoke(*argv):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", ["hub", *argv]), \
                    redirect_stdout(out), redirect_stderr(err):
                try:
                    cli.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code or 0
            return code, out.getvalue(), err.getvalue()

        with tempfile.TemporaryDirectory() as config_home, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": config_home,
                "HUB_CONFIG_FILE": "",
                "HUB_URL": "",
                "HUB_BASE_URL": "",
                "HUB_TOKEN": "",
                "HUB_JOB_TOKEN": "",
        }), patch.object(worker_auth, "config_path", return_value=None), \
                patch.object(api, "safe_http_json", side_effect=response_for):
            config.save_human_config({
                "client_kind": "human",
                "base_url": "https://hub.test",
                "access_token": "human-token",
            })

            auth_code, auth_out, auth_err = invoke("auth", "status")
            sessions_code, sessions_out, sessions_err = invoke("sessions", "list")
            machines_code, machines_out, machines_err = invoke("machines", "available")

        self.assertEqual(auth_code, 0, auth_err)
        self.assertIn("Status: Logged in as human.", auth_out)
        self.assertEqual(sessions_code, 0, sessions_err)
        self.assertNotIn("Failed to list sessions", sessions_err)
        self.assertEqual(machines_code, 0, machines_err)
        self.assertEqual(machines_out.splitlines(), ["grok"])
        self.assertEqual(
            [(req.full_url, req.get_header("Authorization")) for req in requests],
            [
                ("https://hub.test/api/auth/me", "Bearer human-token"),
                ("https://hub.test/api/sessions?live=1&limit=100", "Bearer human-token"),
                ("https://hub.test/api/fleet/routing/machines", "Bearer human-token"),
            ],
        )

    def test_logged_out_machines_fails_with_clear_login_guidance(self):
        from agent_hub_host import api, cli, worker_auth

        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as config_home, patch.dict(os.environ, {
                "XDG_CONFIG_HOME": config_home,
                "HUB_CONFIG_FILE": "",
                "HUB_URL": "",
                "HUB_BASE_URL": "",
                "HUB_TOKEN": "",
                "HUB_JOB_TOKEN": "",
        }), patch.object(worker_auth, "config_path", return_value=None), \
                patch.object(sys, "argv", ["hub", "machines", "available"]), \
                patch.object(api, "safe_http_json") as request, \
                redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code or 0

        self.assertEqual(code, 1)
        self.assertIn("Failed to get available machines", err.getvalue())
        self.assertIn("Not logged in. Run `hub auth login` to sign in.", err.getvalue())
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
