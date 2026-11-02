import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


class TestMachinesAvailable(unittest.TestCase):
    """Regression for audit e2501fde: the Worker returns {"machines": [...]}."""

    def run_cli(self, response, *extra):
        from agent_hub_host import cli, config, api
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["hub", "machines", "available", *extra]), \
                patch.object(config, "load_config", return_value={"base_url": "https://hub.test"}), \
                patch.object(api, "get_available_machines", return_value=response), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
        return code, out.getvalue(), err.getvalue()

    def test_worker_machines_shape_renders_plain_ids(self):
        code, out, err = self.run_cli({"machines": [{"id": "hetzner"}, {"hostname": "desktop"}]})
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["hetzner", "desktop"])
        self.assertNotIn("reachable: none", err)

    def test_worker_machines_shape_json(self):
        code, out, _ = self.run_cli({"machines": [{"id": "hetzner"}]}, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"count": 1, "reachable": [{"id": "hetzner"}]})

    def test_empty_machines_reports_none(self):
        code, out, err = self.run_cli({"machines": []})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("reachable: none", err)

    def test_list_and_reachable_shapes_still_accepted(self):
        self.assertEqual(self.run_cli([{"id": "a"}])[1].splitlines(), ["a"])
        self.assertEqual(self.run_cli({"reachable": [{"id": "b"}], "count": 1})[1].splitlines(), ["b"])

    def test_unknown_shape_fails_loudly(self):
        code, _, err = self.run_cli({"unexpected": True})
        self.assertEqual(code, 1)
        self.assertIn("unexpected /api/machines/available response shape", err)


if __name__ == "__main__":
    unittest.main()
