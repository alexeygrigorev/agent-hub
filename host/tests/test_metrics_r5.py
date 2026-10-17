"""M9 R5 targeted regressions for confirmed R3 (DTO parity) and R4 (evidence) findings.

Synthetic fixtures only; no Dashboard private paths. Invalid envelopes must
raise before config/credentials/HTTP with ZERO sink requests. Sub-millisecond
and unknown-empty-bucket DTOs the Worker accepts must validate locally.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
from unittest import mock

HOST_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HOST_ROOT not in sys.path:
    sys.path.insert(0, HOST_ROOT)

from agent_hub_host import metrics  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_metrics_r4 import _post_guarded  # noqa: E402

_PAST = "2026-10-16T12:00:00.123Z"


def _base(key="r5:parity-001", as_of=_PAST):
    return {
        "schema_version": "1.0",
        "as_of": as_of,
        "request_key": key,
        "coverage": {"utilization": "missing", "usage": "missing", "accepted": "missing"},
        "gaps": [{"code": "missing"}],
        "utilization": {"unknown": True, "projects": {}},
        "usage": {"unknown": True, "projects": {}},
        "accepted": {
            "unknown": True,
            "accepted_outcomes": None,
            "task_count": None,
            "commit_count": None,
            "review_count": None,
            "projects": {},
        },
    }


def _buckets(as_of_iso, tz_suffix, unknown=False):
    as_of = metrics.parse_utc(as_of_iso)
    assert as_of is not None
    rows = []
    for i in range(24):
        b0 = as_of - datetime.timedelta(hours=24 - i)
        b1 = as_of - datetime.timedelta(hours=24 - i - 1)
        fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}" + tz_suffix
        rows.append({
            "bucket_start": fmt(b0),
            "bucket_end": fmt(b1),
            "active_agents": None if unknown else 0,
            "agent_hours": None if unknown else 0,
        })
    return rows


class MetricsR5ParityTests(unittest.TestCase):
    def test_ascii_full_string_utc_rejects_unicode_digits_and_trailing_newline(self):
        # R5 R3: \d must not accept Unicode digits; the terminator must not
        # permit a trailing newline. Both raise before config/HTTP (Worker 400).
        for stamp in ("٢٠٢٦-١٠-١٦T١٢:٠٠:٠٠Z", "2026-10-16T12:00:00.123Z\n"):
            self.assertIsNone(metrics.parse_utc(stamp), repr(stamp))
            s = _base()
            s["as_of"] = stamp
            requests, config_calls, error = _post_guarded(s)
            self.assertEqual(requests, 0, repr(stamp))
            self.assertEqual(config_calls, 0, repr(stamp))
            self.assertIsInstance(error, metrics.MetricsError, repr(stamp))

    def test_valid_fraction_spellings_validate(self):
        # R5 R3: all 20 no-fraction/1-9-digit-fraction spellings with Z and
        # +00:00 keep validating, with receiver millisecond semantics.
        for digits in ("", "1", "12", "123", "1234", "12345", "123456",
                       "1234567", "12345678", "123456789"):
            for tz in ("Z", "+00:00"):
                stamp = "2026-10-16T12:00:00" + ("." + digits if digits else "") + tz
                parsed = metrics.parse_utc(stamp)
                self.assertIsNotNone(parsed, stamp)
                self.assertEqual(parsed.microsecond % 1000, 0, stamp)
                s = _base(key="r5:frac-" + (digits or "0") + ("-off" if tz != "Z" else "-z"))
                s["as_of"] = stamp
                metrics.validate_snapshot_payload(dict(s))

    def test_explicit_null_host_rejected_before_config(self):
        # R5 R3: explicit host_id:null is universally different from omitted
        # host identity (Worker 403). Omission still posts past validation.
        s = _base()
        s["host_id"] = None
        requests, config_calls, error = _post_guarded(s)
        self.assertEqual(requests, 0)
        self.assertEqual(config_calls, 0)
        self.assertIsInstance(error, metrics.MetricsError)
        ok = _base(key="r5:omitted-host-ok")
        requests, config_calls, error = _post_guarded(ok)
        self.assertEqual(config_calls, 1)
        self.assertNotIsInstance(error, metrics.MetricsError)

    def test_submillisecond_boundaries_validate(self):
        # R5 R3: the receiver normalizes to milliseconds, so .123456789 as_of
        # against .123 bucket boundaries (and vice versa) must validate.
        s = _base(key="r5:sub-asof", as_of="2026-10-16T12:00:00.123456789Z")
        s["utilization"]["projects"]["agent-branches"] = {
            "unknown": False, "unique_agents": 0, "total_agent_hours": 0,
            "coverage": 1, "unattributed_agent_hours": 0,
            "hourly_buckets": _buckets("2026-10-16T12:00:00.123456789Z", "Z"),
        }
        metrics.validate_snapshot_payload(dict(s))
        t = _base(key="r5:sub-buckets", as_of="2026-10-16T12:00:00.123Z")
        t["utilization"]["projects"]["agent-branches"] = {
            "unknown": False, "unique_agents": 0, "total_agent_hours": 0,
            "coverage": 1, "unattributed_agent_hours": 0,
            "hourly_buckets": _buckets("2026-10-16T12:00:00.123Z", "+00:00"),
        }
        # Rewrite one boundary pair with 9-digit sub-millisecond precision.
        t["utilization"]["projects"]["agent-branches"]["hourly_buckets"] = [
            {**b,
             "bucket_start": b["bucket_start"].replace(".123+00:00", ".123456789+00:00"),
             "bucket_end": b["bucket_end"].replace(".123+00:00", ".123456789+00:00")}
            for b in t["utilization"]["projects"]["agent-branches"]["hourly_buckets"]
        ]
        metrics.validate_snapshot_payload(dict(t))

    def test_unknown_allnull_empty_buckets_validate(self):
        # R5 R3: a native unknown utilization project with all counters null
        # and an empty bucket list validates (receiver fills it); a known
        # project with an empty list still rejects.
        s = _base(key="r5:unknown-empty")
        s["utilization"]["projects"]["agent-branches"] = {
            "unknown": True, "unique_agents": None, "total_agent_hours": None,
            "coverage": None, "unattributed_agent_hours": None,
            "hourly_buckets": [],
        }
        metrics.validate_snapshot_payload(dict(s))
        bad = _base(key="r5:known-empty")
        bad["utilization"]["projects"]["agent-branches"] = {
            "unknown": False, "unique_agents": 0, "total_agent_hours": 0,
            "coverage": 1, "unattributed_agent_hours": 0,
            "hourly_buckets": [],
        }
        requests, config_calls, error = _post_guarded(bad)
        self.assertEqual(requests, 0)
        self.assertEqual(config_calls, 0)
        self.assertIsInstance(error, metrics.MetricsError)


class MetricsR5EvidenceTests(unittest.TestCase):
    def _report(self, projects):
        from test_metrics import _as_of  # noqa: E402

        return {"as_of": _as_of().isoformat(), "unknown": False, "projects": projects}

    def test_malformed_project_keeps_review_total_null(self):
        # R5 R4: one valid feature/task/commit carrying review event r1 in
        # agent-branches plus [{}] in agent-dashboard. The malformed project
        # becomes unknown; the aggregate review total stays null while proven
        # feature/task/commit counts survive.
        from test_metrics import _as_of, _hourly, _usage  # noqa: E402

        report = self._report({
            "agent-branches": [
                {"feature_id": "f1", "task_id": "t1", "commit": "c1", "review_id": "r1"},
            ],
            "agent-dashboard": [{}],
            "quota-launcher": [],
            "agent-coordination": [],
        })
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        proj = snap["accepted"]["projects"]
        self.assertFalse(proj["agent-branches"]["unknown"])
        self.assertEqual(proj["agent-branches"]["review_count"], 1)
        self.assertTrue(proj["agent-dashboard"]["unknown"])
        self.assertIsNone(proj["agent-dashboard"]["review_count"])
        self.assertEqual(snap["accepted"]["task_count"], 1)
        self.assertEqual(snap["accepted"]["commit_count"], 1)
        self.assertIsNone(snap["accepted"]["review_count"])

    def test_commit_only_evidence_cannot_invent_review_zero(self):
        # R5 R4: a nonempty entry with only primitive commit proof keeps
        # review unknown (never known 0); genuine empty input stays known 0.
        from test_metrics import _as_of, _hourly, _usage  # noqa: E402

        report = self._report({
            "agent-branches": [{"commit": "c1"}],
            "agent-dashboard": [],
            "quota-launcher": [],
            "agent-coordination": [],
        })
        snap = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), report)
        self.assertEqual(snap["accepted"]["commit_count"], 1)
        self.assertIsNone(snap["accepted"]["review_count"])
        self.assertIsNone(snap["accepted"]["projects"]["agent-branches"]["review_count"])
        empty = self._report({
            "agent-branches": [],
            "agent-dashboard": [],
            "quota-launcher": [],
            "agent-coordination": [],
        })
        zero = metrics.project_snapshot(_hourly(_as_of()), _usage(_as_of()), empty)
        self.assertEqual(zero["accepted"]["review_count"], 0)


if __name__ == "__main__":
    unittest.main()
