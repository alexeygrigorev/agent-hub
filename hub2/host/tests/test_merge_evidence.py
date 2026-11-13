import json
import subprocess
import unittest
from datetime import datetime, timezone

from agent_hub_host.merge_evidence import collect
from agent_hub_host.git_noninteractive import noninteractive_env


class MergeEvidenceTests(unittest.TestCase):
    def test_git_environment_never_prompts(self):
        env = noninteractive_env({"GIT_CONFIG_COUNT": "2"})
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        self.assertEqual(env["GIT_ASKPASS"], "true")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")

    def test_collects_merged_pr_exact_head_checks_and_merge_ancestry(self):
        head = "a" * 40
        merge = "b" * 40
        main = "c" * 40
        calls = []
        combined_state = "pending"
        statuses = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(argv, 0, "/repo\n", "")
            if argv == ["git", "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(argv, 0, "git@github.com:org/repo.git\n", "")
            if argv[:3] == ["gh", "pr", "view"]:
                result = {"url": "https://github.com/org/repo/pull/23", "state": "MERGED", "baseRefName": "main",
                          "headRefOid": head, "mergedAt": "2026-11-13T10:00:00Z", "mergeCommit": {"oid": merge}}
                return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
            if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
                runs = [{"check_runs": [{"status": "completed", "conclusion": "success", "name": "CI"},
                                         {"status": "completed", "conclusion": "skipped", "name": "deploy"},
                                         {"status": "completed", "conclusion": "success", "name": "Review"}]}]
                return subprocess.CompletedProcess(argv, 0, json.dumps(runs), "")
            if argv[:2] == ["gh", "api"] and argv[-1].endswith("/status"):
                return subprocess.CompletedProcess(argv, 0, json.dumps({"state": combined_state, "statuses": statuses}), "")
            if argv == ["git", "fetch", "--quiet", "origin", "main"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv == ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                return subprocess.CompletedProcess(argv, 0, main + "\n", "")
            if argv == ["git", "merge-base", "--is-ancestor", merge, "refs/remotes/origin/main"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(argv)

        evidence = collect("/repo", "23", runner,
                           lambda: datetime(2026, 11, 13, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(evidence["kind"], "merge_gate")
        self.assertEqual(evidence["repository"], "org/repo")
        self.assertEqual(evidence["pr_url"], "https://github.com/org/repo/pull/23")
        self.assertEqual(evidence["base_ref"], "main")
        self.assertEqual(evidence["head_sha"], head)
        self.assertEqual(evidence["checks_head_sha"], head)
        self.assertEqual(evidence["checks_state"], "success")
        self.assertEqual(evidence["checks_total"], 3)
        self.assertEqual(evidence["checks_successful"], 3)
        self.assertEqual(evidence["checks_failed"], 0)
        self.assertEqual(evidence["merge_commit_sha"], merge)
        self.assertEqual(evidence["main_head_sha"], main)
        self.assertTrue(evidence["merge_commit_in_main"])
        self.assertNotIn("observation_error", evidence)
        self.assertEqual(evidence["checked_at"], "2026-11-13T10:01:00Z")
        self.assertTrue(any("/commits/" + head + "/check-runs" in " ".join(call) for call in calls))
        statuses = [{"state": "pending", "context": "external/ci"}]
        pending = collect("/repo", "23", runner,
                          lambda: datetime(2026, 11, 13, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(pending["checks_state"], "pending", "non-empty pending commit status must keep checks pending")

    def test_unavailable_pr_or_checks_are_recorded_as_failed_observations(self):
        def runner(argv, **kwargs):
            if argv == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(argv, 0, "/repo\n", "")
            if argv == ["git", "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(argv, 0, "https://github.com/org/repo.git\n", "")
            if argv[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(argv, 1, "", "private diagnostic")
            if argv == ["git", "fetch", "--quiet", "origin", "main"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv == ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
                return subprocess.CompletedProcess(argv, 0, "c" * 40, "")
            raise AssertionError(argv)

        evidence = collect("/repo", runner=runner)
        self.assertEqual(evidence["repository"], "org/repo")
        self.assertIn("pull request could not be read", evidence["observation_error"])
        self.assertNotIn("private diagnostic", json.dumps(evidence))
        self.assertFalse(evidence["merge_commit_in_main"])

    def test_non_github_origin_fails_closed_without_running_gh(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(argv, 0, "/repo\n", "")
            if argv == ["git", "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(argv, 0, "https://example.invalid/repo.git\n", "")
            raise AssertionError(argv)

        evidence = collect("/repo", runner=runner)
        self.assertEqual(evidence["repository"], "unknown/unknown")
        self.assertEqual(evidence["observation_error"], "origin is not a GitHub repository")
        self.assertFalse(any(call and call[0] == "gh" for call in calls))


if __name__ == "__main__":
    unittest.main()
