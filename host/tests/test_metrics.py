"""Host adapter projection tests. Fixtures only; no default private Dashboard paths."""

from __future__ import annotations

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HOST_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HOST_ROOT not in sys.path:
    sys.path.insert(0, HOST_ROOT)

from agent_hub_host import metrics
from agent_hub_host.metrics import MetricsError, project_snapshot, snapshot_from_files

SECRET = "SECRET_MARKER_PRIVATE"
DASHBOARD_SRC = "/home/alexey/git/agent-dashboard/src"


def _as_of():
    return datetime.datetime(2026, 10, 17, 12, 0, tzinfo=datetime.timezone.utc)


def _hourly(as_of, unknown=False, extra_project=None):
    start = as_of - datetime.timedelta(hours=24)
    buckets = []
    for i in range(24):
        b0 = start + datetime.timedelta(hours=i)
        b1 = start + datetime.timedelta(hours=i + 1)
        buckets.append({
            "bucket_start": b0.isoformat(),
            "bucket_end": b1.isoformat(),
            "active_agents": None if unknown else (1 if i == 23 else 0),
            "agent_hours": None if unknown else (0.25 if i == 23 else 0.0),
        })
    project = {
        "unknown": unknown,
        "unique_agents": None if unknown else 1,
        "total_agent_hours": None if unknown else 0.25,
        "coverage": None if unknown else 0.01,
        "unattributed_agent_hours": None if unknown else 0,
        "hourly_buckets": buckets,
        "sources": {"hourly": {"path": f"/var/{SECRET}/spans.json"}},
    }
    projects = {
        "agent-branches": project,
        "agent-dashboard": {**project, "unknown": True, "unique_agents": None, "total_agent_hours": None,
                            "coverage": None, "unattributed_agent_hours": None,
                            "hourly_buckets": [{**b, "active_agents": None, "agent_hours": None} for b in buckets]},
        "quota-launcher": {**project, "unknown": True, "unique_agents": None, "total_agent_hours": None,
                           "coverage": None, "unattributed_agent_hours": None,
                           "hourly_buckets": [{**b, "active_agents": None, "agent_hours": None} for b in buckets]},
        "agent-coordination": {**project, "unknown": True, "unique_agents": None, "total_agent_hours": None,
                               "coverage": None, "unattributed_agent_hours": None,
                               "hourly_buckets": [{**b, "active_agents": None, "agent_hours": None} for b in buckets]},
    }
    if extra_project:
        projects[extra_project] = project
    return {
        "as_of": as_of.isoformat(),
        "unknown": False,
        "projects": projects,
        "shared_agent_ids": ["session-secret"],
        "sources": {"hourly": {"path": f"/home/{SECRET}/metrics"}},
    }


def _usage(as_of):
    empty = {
        "unknown": True,
        "total_input_tokens": None,
        "total_output_tokens": None,
        "total_cache_read_tokens": None,
        "total_cache_creation_tokens": None,
        "total_reasoning_tokens": None,
        "total_noncache_input_tokens": None,
        "has_unknown_tokens": True,
        "has_unknown_cache": True,
        "has_unknown_reasoning": True,
        "cache_coverage": None,
        "reasoning_coverage": None,
        "event_count": 0,
        "total_quota_delta_percent": 40.0,
        "total_cost": 1.25,
    }
    known = dict(empty)
    known.update({
        "unknown": False,
        "total_input_tokens": 10,
        "total_output_tokens": 4,
        "has_unknown_tokens": False,
        "event_count": 1,
        "total_quota_delta_percent": 40.0,
        "total_cost": 1.25,
    })
    return {
        "as_of": as_of.isoformat(),
        "unknown": False,
        "projects": {
            "agent-branches": known,
            "agent-dashboard": dict(empty),
            "quota-launcher": dict(empty),
            "agent-coordination": dict(empty),
        },
        "sources": {"usage_events": {"path": f"/tmp/{SECRET}/usage-events.jsonl"}},
    }


def _features(as_of):
    return {
        "as_of": as_of.isoformat(),
        "unknown": False,
        "projects": {
            "agent-branches": [
                {
                    "feature_id": "feat-1",
                    "task_id": "task-1",
                    "commit": "abc",
                    "reviewer": "reviewer-tag",
                    "summary": f"do not leak {SECRET}",
                    "pr_url": "https://example.invalid/pr/1",
                },
                {"feature_id": "feat-1", "task_id": "task-1", "commit": "abc", "reviewer": "reviewer-tag"},
            ]
        },
    }


class MetricsProjectionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self._tmpdir.name
        os.environ.pop("HUB_CONFIG_FILE", None)

    def tearDown(self):
        if self._old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old_xdg
        self._tmpdir.cleanup()

    def test_secret_marker_absent_from_snapshot_and_errors(self):
        as_of = _as_of()
        snap = project_snapshot(_hourly(as_of), _usage(as_of), _features(as_of), host_id="mach1", request_key="metrics:fixture-1")
        dumped = json.dumps(snap)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("/home/", dumped)
        self.assertNotIn("session-secret", dumped)
        self.assertNotIn("total_quota_delta_percent", dumped)
        self.assertNotIn("total_cost", dumped)
        self.assertNotIn("pr_url", dumped)
        self.assertEqual(snap["accepted"]["accepted_outcomes"], 1)
        self.assertEqual(snap["accepted"]["task_count"], 1)
        self.assertEqual(snap["accepted"]["commit_count"], 1)
        # F3 correction: reviewer label alone is not review-event proof; no
        # review_id present, so review_count stays null while outcomes remain.
        self.assertIsNone(snap["accepted"]["review_count"])
        self.assertEqual(len(snap["utilization"]["projects"]["agent-branches"]["hourly_buckets"]), 24)
        self.assertTrue(snap["utilization"]["projects"]["agent-dashboard"]["unknown"])
        self.assertEqual(snap["utilization"]["projects"]["agent-dashboard"]["unique_agents"], None)

    def test_unsupported_project_is_gap_not_canonical_attribution(self):
        as_of = _as_of()
        hourly = _hourly(as_of, extra_project="some-other-repo")
        snap = project_snapshot(hourly, _usage(as_of), _features(as_of), request_key="metrics:fixture-2")
        self.assertIn({"code": "unsupported_project"}, snap["gaps"])
        self.assertIn("unattributed", snap["utilization"]["projects"])
        self.assertTrue(snap["utilization"]["projects"]["unattributed"]["unknown"])
        self.assertIsNone(snap["utilization"]["projects"]["unattributed"]["unique_agents"])
        self.assertNotIn("some-other-repo", snap["utilization"]["projects"])

    def test_quota_percent_is_not_converted_to_tokens(self):
        as_of = _as_of()
        snap = project_snapshot(_hourly(as_of), _usage(as_of), _features(as_of), request_key="metrics:fixture-3")
        usage = snap["usage"]["projects"]["agent-branches"]
        self.assertEqual(usage["total_input_tokens"], 10)
        self.assertEqual(usage["total_output_tokens"], 4)
        self.assertIsNone(usage.get("total_quota_delta_percent"))

    def test_file_loader_does_not_echo_secret_on_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json " + SECRET)
            with self.assertRaises(MetricsError):
                snapshot_from_files(hourly_path=path)
            try:
                snapshot_from_files(hourly_path=path)
            except MetricsError as exc:
                self.assertNotIn(SECRET, str(exc))

    def test_cli_dry_run_hides_marker(self):
        as_of = _as_of()
        with tempfile.TemporaryDirectory() as tmp:
            hourly_path = os.path.join(tmp, "hourly.json")
            usage_path = os.path.join(tmp, "usage.json")
            features_path = os.path.join(tmp, "features.json")
            for path, payload in (
                (hourly_path, _hourly(as_of)),
                (usage_path, _usage(as_of)),
                (features_path, _features(as_of)),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
            from agent_hub_host import cli
            buf = io.StringIO()
            err = io.StringIO()
            with mock.patch.object(sys, "argv", [
                "hub", "metrics", "post", "--hourly", hourly_path, "--usage", usage_path,
                "--features", features_path, "--key", "metrics:cli-1", "--dry-run",
            ]), mock.patch.object(sys, "stdout", buf), mock.patch.object(sys, "stderr", err):
                cli.main()
            out = buf.getvalue()
            self.assertNotIn(SECRET, out)
            self.assertIn("accepted_outcomes", out)
            self.assertNotIn(SECRET, err.getvalue())

    @unittest.skipUnless(os.path.isdir(DASHBOARD_SRC), "dashboard source unavailable")
    def test_readonly_producer_apis_with_synthetic_inputs(self):
        if DASHBOARD_SRC not in sys.path:
            sys.path.insert(0, DASHBOARD_SRC)
        from dashboard.hourly import compute_hourly_utilization
        from dashboard.accounting import aggregate_project_usage
        from dashboard.features import load_accepted_features

        as_of = _as_of()
        spans = [{
            "project_id": "agent-branches",
            "agent_id": "worker-1",
            "started_at": (as_of - datetime.timedelta(hours=1)).isoformat(),
            "ended_at": as_of.isoformat(),
        }]
        hourly = compute_hourly_utilization(spans, as_of=as_of)
        usage = aggregate_project_usage([
            {
                "project_id": "agent-branches",
                "provider": "grok",
                "response_id": "r1",
                "conversation_id": "c1",
                "session_uuid": "s1",
                "device_id": "d1",
                "timestamp": (as_of - datetime.timedelta(minutes=30)).isoformat(),
                "input_tokens": 8,
                "output_tokens": 2,
                "quota_delta": 5.0,
                "cost": 0.01,
            }
        ], as_of=as_of)
        with tempfile.TemporaryDirectory() as tmp:
            tasks_path = os.path.join(tmp, "TASKS.json")
            with open(tasks_path, "w", encoding="utf-8") as handle:
                json.dump({"tasks": [{
                    "id": "t1",
                    "feature_id": "f1",
                    "project_id": "agent-branches",
                    "status": "ACCEPTED",
                    "accepted_at": (as_of - datetime.timedelta(hours=2)).isoformat(),
                    "commit": "deadbeef",
                    "tests": ["unit"],
                    "reviewer": "rev",
                }]}, handle)
            features = load_accepted_features(tasks_path, as_of=as_of)
        snap = project_snapshot(hourly, usage, features, request_key="metrics:producer-1")
        dumped = json.dumps(snap)
        self.assertNotIn("worker-1", dumped)
        self.assertNotIn("deadbeef", dumped)
        self.assertNotIn("c1", dumped)
        self.assertEqual(len(snap["utilization"]["projects"]["agent-branches"]["hourly_buckets"]), 24)
        self.assertIsNone(snap["usage"]["projects"]["agent-branches"].get("total_cost"))
        self.assertGreaterEqual(snap["accepted"]["accepted_outcomes"] or 0, 1)


if __name__ == "__main__":
    unittest.main()
