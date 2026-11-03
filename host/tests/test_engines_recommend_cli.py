import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_hub_host import cli, provisioning

RESULT = {
    "tier": "daily",
    "selected": {"machine": "laptop", "engine": "antigravity", "account": "hub-antigravity"},
    "candidates": [
        {"machine": "laptop", "engine": "antigravity", "account": "hub-antigravity", "model_id": "argon",
         "normalized_score": 100.0, "decision_score": 7.4e-4,
         "decision_reason": "auto: antigravity/hub-antigravity (argon) on laptop score=7.40e-4 norm=100/100 [rem=41%]",
         "decision": {"factors": {"remaining_pct": 41, "burn_ratio": 0.72, "burn_factor": 0.72, "load_factor": 0.19,
                                   "host_factor": 0.7, "failure_factor": 1.0, "recent_failures": 0}}},
        {"machine": "hetzner", "engine": "antigravity", "account": "hub-antigravity", "model_id": "argon",
         "normalized_score": 11.4, "decision_score": 8.4e-5,
         "decision_reason": "auto: antigravity/hub-antigravity (argon) on hetzner score=8.40e-5 norm=11.4/100 [rem=41%]"},
    ],
    "rejected": [{"machine": "desktop", "engine": "codex", "reason": "desktop_codex_excluded"},
                 {"machine": "desktop", "engine": "claude", "reason": "desktop_codex_excluded"}],
}


def run_cli(argv, result=RESULT):
    out = io.StringIO()
    with mock.patch("sys.argv", ["hub"] + argv), \
            mock.patch("agent_hub_host.bus._do_request", return_value=result) as req, \
            redirect_stdout(out):
        cli.main()
    return out.getvalue(), req


class EnginesRecommendCliTests(unittest.TestCase):
    def test_passes_tier_repo_and_project_to_the_recommend_api(self):
        _, req = run_cli(["engines", "recommend", "--tier", "deep", "--repo", "DataTalksClub/agent-hub", "--project", "agent-hub"])
        req.assert_called_once_with(
            "GET", "/api/fleet/recommend?tier=deep&project=agent-hub&repo=DataTalksClub%2Fagent-hub&mode=headless")

    def test_prints_the_ranked_candidates_with_scores_and_reasons(self):
        text, _ = run_cli(["engines", "recommend"])
        lines = text.splitlines()
        self.assertIn("1.", lines[1])
        self.assertIn("100.0", lines[1])
        self.assertIn("antigravity/hub-antigravity (argon) on laptop  <- selected", lines[1])
        self.assertIn("norm=100/100", text)
        self.assertIn("2.  11.4", text)
        self.assertIn("host=0.7x", text)
        self.assertIn("excluded: desktop_codex_excluded x2", text)

    def test_json_flag_prints_the_raw_response(self):
        text, _ = run_cli(["engines", "recommend", "--json"])
        self.assertEqual(json.loads(text), RESULT)

    def test_pinned_engine_needs_a_reason(self):
        with self.assertRaises(ValueError):
            provisioning.command(mock.Mock(engines_cmd="recommend", engine="claude", reason=None))

    def test_empty_result_is_stated(self):
        text = provisioning.render_recommend({"candidates": [], "rejected": []})
        self.assertEqual(text, "no eligible candidate")


if __name__ == "__main__":
    unittest.main()
