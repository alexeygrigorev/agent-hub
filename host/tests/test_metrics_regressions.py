"""Regression tests for M9 R4–R6 and POST validation. Original assertions stay in test_metrics.py."""

from __future__ import annotations

import datetime
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

sys.path.insert(0, os.path.dirname(__file__))
from test_metrics import DASHBOARD_SRC, _as_of, _features, _hourly, _usage  # noqa: E402


class MetricsRegressionTests(unittest.TestCase):
    def test_requires_as_of_for_each_supplied_report(self):
        use = _usage(_as_of())
        del use["as_of"]
        with self.assertRaises(metrics.MetricsError):
            metrics.project_snapshot(_hourly(_as_of()), use, _features(_as_of()))

    def test_rejects_mismatched_window_endpoints(self):
        hourly = _hourly(_as_of())
        hourly["window_start"] = (_as_of() - datetime.timedelta(hours=12)).isoformat()
        hourly["window_end"] = _as_of().isoformat()
        with self.assertRaises(metrics.MetricsError):
            metrics.project_snapshot(hourly, _usage(_as_of()), _features(_as_of()))

    def test_preserves_proven_zero_accepted_outcomes(self):
        report = {
            "as_of": _as_of().isoformat(),
            "unknown": False,
            "projects": {},
        }
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertFalse(snap["accepted"]["unknown"])
        self.assertEqual(snap["accepted"]["accepted_outcomes"], 0)
        self.assertEqual(snap["accepted"]["task_count"], 0)
        self.assertEqual(snap["accepted"]["commit_count"], 0)
        self.assertEqual(snap["accepted"]["review_count"], 0)

    def test_two_features_with_one_reviewer_are_two_reviews(self):
        features = _features(_as_of())
        features["projects"]["agent-branches"] = [
            {"feature_id": "f1", "task_id": "t1", "commit": "c1", "reviewer": "same-reviewer"},
            {"feature_id": "f2", "task_id": "t2", "commit": "c2", "reviewer": "same-reviewer"},
        ]
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), features)
        self.assertEqual(snap["accepted"]["accepted_outcomes"], 2)
        self.assertEqual(snap["accepted"]["task_count"], 2)
        self.assertEqual(snap["accepted"]["commit_count"], 2)
        # F3 correction: shared reviewer label does not establish distinct
        # review events; without agreed review-event identity, keep null.
        self.assertIsNone(snap["accepted"]["review_count"])

    def test_review_ids_are_counted_and_missing_proof_is_null(self):
        features = _features(_as_of())
        features["projects"]["agent-branches"] = [
            {"feature_id": "f1", "task_id": "t1", "commit": "c1", "review_id": "rev-1"},
            {"feature_id": "f2", "task_id": "t2", "commit": "c2", "review_id": "rev-1"},
        ]
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), features)
        self.assertEqual(snap["accepted"]["review_count"], 1)
        missing = _features(_as_of())
        missing["projects"]["agent-branches"] = [
            {"feature_id": "f9", "task_id": "t9", "commit": "c9"},
        ]
        snap_missing = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), missing)
        self.assertIsNone(snap_missing["accepted"]["review_count"])

    def test_malformed_project_entries_do_not_invent_counts(self):
        features = _features(_as_of())
        features["projects"]["agent-branches"] = {"not": "a-list"}
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), features)
        self.assertTrue(snap["accepted"]["projects"]["agent-branches"]["unknown"])
        self.assertIsNone(snap["accepted"]["projects"]["agent-branches"]["accepted_outcomes"])

    def test_preserves_supplied_source_coverage_gaps(self):
        use = _usage(_as_of())
        known = use["projects"]["agent-branches"]
        use["projects"] = {p: dict(known) for p in metrics.CANONICAL_PROJECT_IDS}
        use["coverage_gaps"] = [{"source": "provenance", "reason": "SYNTHETIC_REVIEW_MARKER"}]
        snap = metrics.project_snapshot(_hourly(_as_of()), use, _features(_as_of()))
        dumped = json.dumps(snap)
        self.assertIn(snap["coverage"]["usage"], ["partial", "unknown"])
        self.assertNotIn("SYNTHETIC_REVIEW_MARKER", dumped)
        self.assertIn({"code": "partial_coverage"}, snap["gaps"])

    def test_validates_snapshot_before_network_post(self):
        with mock.patch.object(metrics.config, "load_config", return_value={}), mock.patch.object(
            metrics.api, "safe_http_json", return_value={}
        ) as sent:
            with self.assertRaises(metrics.MetricsError):
                metrics.post_metrics_snapshot(
                    {"unexpected_raw": {"text": "SYNTHETIC_REVIEW_MARKER"}},
                    base_url="http://localhost:1",
                    token="hubt_synthetic_review",
                )
            sent.assert_not_called()

    def test_duplicate_features_remain_one_outcome(self):
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), _features(_as_of()))
        self.assertEqual(snap["accepted"]["accepted_outcomes"], 1)
        # F3 correction: deduplication preserves one outcome; reviewer-only
        # fixture carries no review-event ID, so review_count stays null.
        self.assertIsNone(snap["accepted"]["review_count"])

    def test_ui_names_host_identity_numeric_cache_and_access_denied(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        js_path = os.path.join(root, "public", "metrics.js")
        html_path = os.path.join(root, "public", "metrics.html")
        with open(js_path, encoding="utf-8") as handle:
            js = handle.read()
        with open(html_path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("metrics-host", html)
        self.assertIn("dk-select", html)
        self.assertIn("total_cache_read_tokens", js)
        self.assertIn("total_reasoning_tokens", js)
        self.assertIn("Access denied", js)
        self.assertNotIn("innerHTML", js)

    @unittest.skipUnless(os.path.isdir(DASHBOARD_SRC), "dashboard source unavailable")
    def test_readonly_producer_empty_and_provenance_gap(self):
        if DASHBOARD_SRC not in sys.path:
            sys.path.insert(0, DASHBOARD_SRC)
        from dashboard.accounting import aggregate_project_usage
        from dashboard.features import load_accepted_features

        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "synthetic-tasks.json")
            with open(tasks, "w", encoding="utf-8") as handle:
                handle.write('{"tasks":[]}')
            report = load_accepted_features(tasks, as_of=_as_of())
        self.assertFalse(report["unknown"])
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(snap["accepted"]["accepted_outcomes"], 0)

        records = []
        for i, project in enumerate(metrics.CANONICAL_PROJECT_IDS):
            records.append({
                "project_id": project,
                "provider": "grok",
                "response_id": "r" + str(i),
                "conversation_id": "c" + str(i),
                "session_uuid": "synthetic-session",
                "device_id": "synthetic-device",
                "timestamp": (_as_of() - datetime.timedelta(hours=1)).isoformat(),
                "input_tokens": 3,
                "output_tokens": 2,
            })
        records.append({**records[0], "response_id": "excluded", "session_uuid": None})
        producer = aggregate_project_usage(records, as_of=_as_of())
        self.assertEqual(producer["records_missing_provenance"], 1)
        self.assertTrue(producer["coverage_gaps"])
        snap_usage = metrics.project_snapshot(_hourly(_as_of()), producer, _features(_as_of()))
        dumped = json.dumps(snap_usage)
        self.assertIn(snap_usage["coverage"]["usage"], ["partial", "unknown"])
        self.assertNotIn("synthetic-session", dumped)
        self.assertNotIn("excluded", dumped)


if __name__ == "__main__":
    unittest.main()
