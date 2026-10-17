"""M9 R3 focused regressions for F2 strict DTO and F3 acceptance uncertainty.

Synthetic fixtures only; no Dashboard private paths. Uses real local HTTP sink
to prove invalid envelopes cause zero outbound requests.
"""

from __future__ import annotations

import datetime
import http.server
import json
import os
import sys
import threading
import unittest
from unittest import mock

HOST_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HOST_ROOT not in sys.path:
    sys.path.insert(0, HOST_ROOT)

from agent_hub_host import metrics

sys.path.insert(0, os.path.dirname(__file__))
from test_metrics import _as_of, _hourly, _usage, _features  # noqa: E402

MARK = "SYNTHETIC_PRIVATE_MARKER_R3"


def _snapshot():
    return metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), _features(_as_of()))


class Handler(http.server.BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        self.bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class MetricsR3Tests(unittest.TestCase):
    def test_direct_helper_sends_valid_snapshot(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            s = _snapshot()
            Handler.bodies = []
            with mock.patch.object(metrics.config, "load_config", return_value={}):
                metrics.post_metrics_snapshot(
                    s, base_url="http://127.0.0.1:" + str(server.server_port), token="hubt_synthetic"
                )
            self.assertEqual(len(Handler.bodies), 1)
            self.assertNotIn(MARK.encode(), Handler.bodies[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_direct_helper_blocks_unknown_nested_before_http(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            s = _snapshot()
            s["usage"]["innocent"] = {"detail": MARK}
            Handler.bodies = []
            with mock.patch.object(metrics.config, "load_config", return_value={}):
                with self.assertRaises(metrics.MetricsError):
                    metrics.post_metrics_snapshot(
                        s, base_url="http://127.0.0.1:" + str(server.server_port), token="hubt_synthetic"
                    )
            self.assertEqual(len(Handler.bodies), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_direct_helper_rejects_counts_and_window(self):
        bad = []
        for kind in ["negative", "boolean", "window", "unknown-field"]:
            s = _snapshot()
            p = s["utilization"]["projects"]["agent-branches"]
            if kind == "negative":
                s["accepted"]["accepted_outcomes"] = -1
            if kind == "boolean":
                s["usage"]["projects"]["agent-branches"]["total_input_tokens"] = True
            if kind == "window":
                p["hourly_buckets"][0]["bucket_end"] = s["as_of"]
            if kind == "unknown-field":
                s["accepted"]["projects"]["agent-branches"]["innocent"] = {"detail": MARK}
            with mock.patch.object(metrics.config, "load_config", return_value={}), mock.patch.object(
                metrics.api, "safe_http_json", return_value={}
            ) as sent:
                try:
                    metrics.post_metrics_snapshot(s, base_url="http://127.0.0.1:1", token="hubt_synthetic")
                except metrics.MetricsError:
                    pass
                if sent.called:
                    bad.append(kind)
        self.assertEqual(bad, [])

    def test_rejected_acceptance_is_not_observed_zero(self):
        pytest_producer = None
        try:
            sys.path.insert(0, "/home/alexey/git/agent-dashboard/src")
            from dashboard.features import load_accepted_features

            pytest_producer = load_accepted_features
        except Exception:
            self.skipTest("dashboard producer unavailable")
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(
                {"tasks": [{"id": "rejected", "project_id": "agent-branches", "status": "ACCEPTED",
                            "accepted_at": (_as_of() - datetime.timedelta(hours=1)).isoformat()}]},
                handle,
            )
            path = handle.name
        try:
            report = pytest_producer(path, as_of=_as_of())
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.assertTrue(report["coverage_gaps"])
        s = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertNotEqual(s["coverage"]["accepted"], "observed")
        self.assertIsNone(s["accepted"]["accepted_outcomes"])

    def test_malformed_list_is_unknown_not_zero(self):
        report = {"as_of": _as_of().isoformat(), "unknown": False, "projects": {"agent-branches": ["malformed"]}}
        s = metrics.project_snapshot(features=report)
        self.assertIsNone(s["accepted"]["accepted_outcomes"])
        self.assertNotEqual(s["coverage"]["accepted"], "observed")

    def test_missing_projects_is_unknown_not_zero(self):
        report = {"as_of": _as_of().isoformat(), "unknown": False}
        s = metrics.project_snapshot(features=report)
        self.assertIsNone(s["accepted"]["accepted_outcomes"])

    def test_reviewer_label_alone_is_null(self):
        report = _features(_as_of())
        report["projects"]["agent-branches"] = [
            {"feature_id": "a", "task_id": "a", "commit": "x", "reviewer": "same"},
            {"feature_id": "b", "task_id": "b", "commit": "y", "reviewer": "same"},
        ]
        s = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(s["accepted"]["accepted_outcomes"], 2)
        self.assertIsNone(s["accepted"]["review_count"])

    def test_explicit_review_ids_count(self):
        report = _features(_as_of())
        report["projects"]["agent-branches"] = [
            {"feature_id": "f1", "task_id": "t1", "commit": "c1", "review_id": "rev-1"},
            {"feature_id": "f2", "task_id": "t2", "commit": "c2", "review_id": "rev-1"},
        ]
        s = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(s["accepted"]["review_count"], 1)

    def test_genuine_empty_stays_zero(self):
        report = {"as_of": _as_of().isoformat(), "unknown": False, "projects": {}}
        s = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(s["accepted"]["accepted_outcomes"], 0)
        self.assertEqual(s["coverage"]["accepted"], "observed")


if __name__ == "__main__":
    unittest.main()
