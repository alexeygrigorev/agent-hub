"""M9 R4 targeted regressions for confirmed F2/F3 findings.

Synthetic fixtures only; no Dashboard private paths. Invalid envelopes must
raise before config/credentials/HTTP with ZERO loopback-sink requests.
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


def _post_guarded(snapshot):
    """Post against a loopback sink; return (requests, config_calls, error)."""
    Handler.bodies = []
    with mock.patch.object(metrics.config, "load_config", return_value={}) as cfg:
        error = None
        try:
            metrics.post_metrics_snapshot(
                snapshot,
                base_url="http://127.0.0.1:1",
                token="hubt_synthetic",
            )
        except Exception as exc:  # noqa: BLE001 - record any failure mode
            error = exc
        return len(Handler.bodies), cfg.call_count, error


class MetricsR4ParityTests(unittest.TestCase):
    def test_valid_snapshot_posts_exactly_once(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            Handler.bodies = []
            with mock.patch.object(metrics.config, "load_config", return_value={}):
                metrics.post_metrics_snapshot(
                    _snapshot(),
                    base_url="http://127.0.0.1:" + str(server.server_port),
                    token="hubt_synthetic",
                )
            self.assertEqual(len(Handler.bodies), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_missing_nested_required_keys_rejected_before_http(self):
        # R4 F2: absent usage counters are not explicit null. Each variant
        # must raise before config resolution with zero sink requests.
        variants = []
        for key in (
            "total_input_tokens",
            "total_output_tokens",
            "total_cache_read_tokens",
            "total_cache_creation_tokens",
            "total_reasoning_tokens",
            "total_noncache_input_tokens",
            "has_unknown_tokens",
            "cache_coverage",
            "event_count",
        ):
            s = _snapshot()
            del s["usage"]["projects"]["agent-branches"][key]
            requests, config_calls, error = _post_guarded(s)
            variants.append((key, requests, config_calls, error))
        for key, requests, config_calls, error in variants:
            self.assertEqual(requests, 0, key)
            self.assertEqual(config_calls, 0, key)
            self.assertIsInstance(error, metrics.MetricsError, key)

    def test_non_worker_utc_grammar_rejected_before_http(self):
        # R4 F2: the helper uses the receiver's UTC grammar. Compact basic
        # spellings, lowercase designators, nonzero offsets and naive stamps
        # all raise before config/HTTP, exactly as the Worker 400s them.
        base = _as_of()
        bad_stamps = [
            base.strftime("%Y%m%dT%H%M%SZ"),
            base.strftime("%Y-%m-%dT%H:%M:%S") + "z",
            base.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+01:00",
            base.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            base.strftime("%Y-%m-%dT%H:%M:%S"),
            "  " + base.isoformat() + " ",
        ]
        for stamp in bad_stamps:
            s = _snapshot()
            s["as_of"] = stamp
            requests, config_calls, error = _post_guarded(s)
            self.assertEqual(requests, 0, stamp)
            self.assertEqual(config_calls, 0, stamp)
            self.assertIsInstance(error, metrics.MetricsError, stamp)

    def test_worker_utc_grammar_positives_validate(self):
        # R4 F2: valid extended UTC spellings keep working (Z, +00:00,
        # fractional, explicit null/partial/true-zero projections).
        base = _as_of()
        for stamp in (
            base.strftime("%Y-%m-%dT%H:%M:%SZ"),
            base.isoformat(),
            base.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            base.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00",
        ):
            parsed = metrics.parse_utc(stamp)
            self.assertIsNotNone(parsed, stamp)
            s = _snapshot()
            s["as_of"] = stamp
            metrics.validate_snapshot_payload(dict(s))
        snap = _snapshot()
        self.assertIsNone(snap["usage"]["projects"]["agent-branches"]["total_reasoning_tokens"])
        partial = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), _features(_as_of()))
        self.assertIn(partial["coverage"]["accepted"], ("partial", "unknown", "observed"))
        zero = metrics.project_snapshot(features={"as_of": _as_of().isoformat(), "unknown": False, "projects": {}})
        self.assertEqual(zero["accepted"]["accepted_outcomes"], 0)


class MetricsR4EvidenceTests(unittest.TestCase):
    def test_malformed_object_entries_stay_unknown_not_zero(self):
        # R4 F3: {} / reviewer-only / bool-object identifiers / object
        # review_id are malformed evidence, never observed zero/counts.
        for entry in (
            {},
            {"reviewer": "label"},
            {"feature_id": True, "task_id": {}, "commit": []},
            {"feature_id": "f", "review_id": {"detail": "synthetic-object"}},
        ):
            report = {"as_of": _as_of().isoformat(), "unknown": False, "projects": {"agent-branches": [entry]}}
            snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
            self.assertIsNone(snap["accepted"]["accepted_outcomes"], repr(entry))
            self.assertIsNone(snap["accepted"]["review_count"], repr(entry))
            self.assertTrue(snap["accepted"]["projects"]["agent-branches"]["unknown"], repr(entry))

    def test_missing_review_event_keeps_total_unknown(self):
        # R4 F3: one of two otherwise valid features without review-event
        # proof keeps the review TOTAL null; feature/task/commit counts stay.
        report = _features(_as_of())
        report["projects"]["agent-branches"] = [
            {"feature_id": "f1", "task_id": "t1", "commit": "c1", "review_id": "real-event-1"},
            {"feature_id": "f2", "task_id": "t2", "commit": "c2"},
        ]
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(snap["accepted"]["accepted_outcomes"], 2)
        self.assertEqual(snap["accepted"]["task_count"], 2)
        self.assertEqual(snap["accepted"]["commit_count"], 2)
        self.assertIsNone(snap["accepted"]["review_count"])

    def test_same_review_event_across_projects_counts_once(self):
        # R4 F3: one agreed event in two canonical projects aggregates to 1.
        report = {"as_of": _as_of().isoformat(), "unknown": False, "projects": {p: [] for p in metrics.CANONICAL_PROJECT_IDS}}
        report["projects"]["agent-branches"] = [
            {"feature_id": "f1", "task_id": "t1", "commit": "c1", "review_id": "real-event-1"}
        ]
        report["projects"]["agent-dashboard"] = [
            {"feature_id": "f2", "task_id": "t2", "commit": "c2", "review_id": "real-event-1"}
        ]
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(snap["accepted"]["projects"]["agent-branches"]["review_count"], 1)
        self.assertEqual(snap["accepted"]["projects"]["agent-dashboard"]["review_count"], 1)
        self.assertEqual(snap["accepted"]["review_count"], 1)

    def test_repeated_explicit_event_stays_one_and_genuine_empty_zero(self):
        report = _features(_as_of())
        report["projects"]["agent-branches"] = [
            {"feature_id": "f1", "task_id": "t1", "commit": "c1", "review_id": "rev-1"},
            {"feature_id": "f2", "task_id": "t2", "commit": "c2", "review_id": "rev-1"},
        ]
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(snap["accepted"]["review_count"], 1)
        empty = metrics.project_snapshot(
            features={"as_of": _as_of().isoformat(), "unknown": False, "projects": {}}
        )
        self.assertEqual(empty["accepted"]["accepted_outcomes"], 0)
        self.assertEqual(empty["accepted"]["review_count"], 0)

    def test_EXCLUDED_sparse_project_empty_contract_open(self):
        # EXCLUDED from the verdict per the independent review: a map with
        # only one empty canonical project list fills the other projects
        # with zero (producer uses sparse maps, so this needs contract
        # clarification). Pinned observation only; neither product fix nor
        # acceptance is claimed here. Genuine observed empty {} stays zero.
        report = {"as_of": _as_of().isoformat(), "unknown": False, "projects": {"agent-branches": []}}
        snap = metrics.project_snapshot(features=report)
        self.assertEqual(snap["accepted"]["accepted_outcomes"], 0)


if __name__ == "__main__":
    unittest.main()
