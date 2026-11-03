import argparse
import unittest
from unittest.mock import patch
from agent_hub_host import quota, tbm


class CatalogTests(unittest.TestCase):
    def test_metrics_dispatch_and_unknown_rendering(self):
        parser = argparse.ArgumentParser()
        quota.add_commands(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["quota", "metrics", "--json"])
        data = {"pools": [{"pool_id": "a", "windows": {"7d": {"metrics": {
            "useful_work_per_tokens": {"value": None, "reason": "no_tokens", "unit": "work/MTok"},
            "reserve_floor_breaches": {"value": {"hard": 0}, "unit": "samples"}
        }}}}]}
        with patch("agent_hub_host.session_control.request", return_value=data) as request:
            self.assertEqual(quota.execute(args), data)
            request.assert_called_once_with("GET", "/api/quota/metrics")
        text = tbm.render_text("metrics", data)
        self.assertIn("unknown (no_tokens)", text)
        self.assertIn('"hard": 0', text)
        with self.assertRaises(ValueError):
            quota.execute(parser.parse_args(["quota", "metrics", "--provider", "zai"]))
