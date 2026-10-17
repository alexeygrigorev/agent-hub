"""Metrics producer collector contract: bounded real Dashboard->Hub freshness.

Synthetic only: no live Dashboard, no network, no Hub. Uses the REAL
metrics validator/projector for sanitized DTO checks, fakes exporter and
HTTP. Proves disabled-missing-config, cadence/singleflight, valid submit,
stale/malformed no-fake-freshness, bounds/timeout, and host-loop responsiveness.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

HOST_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if HOST_ROOT not in sys.path:
    sys.path.insert(0, HOST_ROOT)

from agent_hub_host import metrics, metrics_producer, session_host
from agent_hub_host.metrics_producer import MetricsProducer

REPO_TMP = Path(__file__).resolve().parents[2] / ".local" / "tmp"
SECRET = "SECRET_MARKER_PRIVATE"

sys.path.insert(0, os.path.dirname(__file__))
from test_metrics import _as_of, _features, _hourly, _usage  # noqa: E402


def _cfg(**over):
    base = {
        "machine_id": "h",
        "dashboard_src": "/home/alexey/git/agent-dashboard/src",
        "output_dir": "/home/alexey/git/agent-hub-wt-platform-capture-screen/.local/tmp/prod-out",
    }
    base.update(over)
    return base


def _fresh_as_of():
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    # Projector normalizes to milliseconds; keep exact second boundary.
    return now


def _valid_snapshot(as_of=None):
    ao = as_of or _fresh_as_of()
    return metrics.project_snapshot(_hourly(ao), _usage(ao), _features(ao))


class MetricsProducerContractTests(unittest.TestCase):
    def test_missing_config_means_disabled_no_exporter_no_http(self):
        journal = Path(tempfile.mkdtemp(prefix="metrics-disabled-", dir=str(REPO_TMP)))
        self.addCleanup(shutil.rmtree, str(journal), True)
        called = []
        producer = MetricsProducer(
            config_path=str(journal / "metrics-producer.json"),
            machine_id="h",
            exporter_runner=lambda cfg: called.append(1) or _valid_snapshot(),
            submit_fn=lambda snap, **k: called.append("http"),
        )
        result = producer.step(submit=True)
        self.assertTrue(result["disabled"])
        self.assertFalse(result["posted"])
        self.assertIn("disabled", result["errors"])
        self.assertEqual(called, [])

    def test_invalid_config_fails_closed_no_http(self):
        submitted = []
        producer = MetricsProducer(
            config={"machine_id": "h", "dashboard_src": "/tmp/evil", "output_dir": "/tmp/o"},
            exporter_runner=lambda cfg: submitted.append(1),
            submit_fn=lambda snap, **k: submitted.append("http"),
        )
        result = producer.step(submit=True)
        self.assertFalse(result["posted"])
        self.assertIn("invalid_config", result["errors"])
        self.assertEqual(submitted, [])

    def test_cadence_rate_limits_second_step(self):
        now = [1000.0]
        producer = MetricsProducer(
            config=_cfg(),
            exporter_runner=lambda cfg: _valid_snapshot(),
            submit_fn=lambda snap, **k: {"ok": True},
            monotonic_fn=lambda: now[0],
        )
        first = producer.step(submit=False)
        self.assertFalse(first["rate_limited"])
        now[0] += 10.0
        second = producer.step(submit=False)
        self.assertTrue(second["rate_limited"])
        now[0] += 400.0
        third = producer.step(submit=False, force=False)
        self.assertFalse(third["rate_limited"])

    def test_min_interval_floor_60(self):
        self.assertEqual(metrics_producer._clamp_interval(1), 60)
        self.assertEqual(metrics_producer._clamp_interval(30), 60)
        self.assertEqual(metrics_producer._clamp_interval(300), 300)
        normalized = metrics_producer.validate_config(_cfg(min_interval_s=5))
        self.assertEqual(normalized["min_interval_s"], 60)

    def test_valid_sanitized_projection_submit(self):
        submitted = []
        snap = _valid_snapshot()
        self.assertNotIn(SECRET, json.dumps(snap))
        producer = MetricsProducer(
            config=_cfg(),
            exporter_runner=lambda cfg: _valid_snapshot(),
            submit_fn=lambda s, **k: submitted.append(s) or {"ok": True},
        )
        result = producer.step(submit=True)
        self.assertTrue(result["posted"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(submitted), 1)
        # Sanitized: no quota/cost/private marker survives strict validation.
        body = json.dumps(submitted[0])
        self.assertNotIn(SECRET, body)
        self.assertNotIn("total_cost", body)
        self.assertNotIn("total_quota_delta_percent", body)
        metrics.validate_snapshot_payload(submitted[0])

    def test_stale_snapshot_no_fake_freshness(self):
        submitted = []
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
        old = old.replace(microsecond=0)
        snap = _valid_snapshot(as_of=old)
        producer = MetricsProducer(
            config=_cfg(),
            exporter_runner=lambda cfg: snap,
            submit_fn=lambda s, **k: submitted.append(s),
        )
        result = producer.step(submit=True)
        self.assertFalse(result["posted"])
        self.assertIn("stale_snapshot", result["errors"])
        self.assertEqual(submitted, [])

    def test_malformed_snapshot_no_post(self):
        submitted = []
        bad = _valid_snapshot()
        del bad["usage"]["projects"]["agent-branches"]["total_reasoning_tokens"]
        producer = MetricsProducer(
            config=_cfg(),
            exporter_runner=lambda cfg: bad,
            submit_fn=lambda s, **k: submitted.append(s),
        )
        result = producer.step(submit=True)
        self.assertFalse(result["posted"])
        self.assertIn("invalid_snapshot", result["errors"])
        self.assertEqual(submitted, [])

    def test_missing_exporter_output_no_post(self):
        submitted = []
        def _boom(cfg):
            raise FileNotFoundError("gone")
        producer = MetricsProducer(
            config=_cfg(),
            exporter_runner=_boom,
            submit_fn=lambda s, **k: submitted.append(s),
        )
        result = producer.step(submit=True)
        self.assertFalse(result["posted"])
        self.assertTrue(result["errors"])
        self.assertEqual(submitted, [])

    def test_exporter_timeout_class_no_report_data(self):
        submitted = []
        def _timeout(cfg):
            raise TimeoutError("slow")
        producer = MetricsProducer(
            config=_cfg(),
            exporter_runner=_timeout,
            submit_fn=lambda s, **k: submitted.append(s),
        )
        result = producer.step(submit=True)
        self.assertFalse(result["posted"])
        self.assertIn("exporter_timeout", result["errors"])
        self.assertEqual(submitted, [])
        # Failure class only: no argv/stderr/report data in errors.
        self.assertEqual(json.dumps(result["errors"]), '["exporter_timeout"]')

    def test_submit_failure_isolated(self):
        def _fail(snap, **k):
            raise RuntimeError("net down")
        producer = MetricsProducer(
            config=_cfg(),
            exporter_runner=lambda cfg: _valid_snapshot(),
            submit_fn=_fail,
        )
        result = producer.step(submit=True)
        self.assertFalse(result["posted"])
        self.assertIn("submit_failed", result["errors"])

    def test_output_dir_must_be_home_private(self):
        with self.assertRaises(metrics_producer.MetricsProducerError):
            metrics_producer.validate_config(_cfg(output_dir="/tmp/global/data/x"))
        with self.assertRaises(metrics_producer.MetricsProducerError):
            metrics_producer.validate_config(_cfg(output_dir="/tmp/out"))
        with self.assertRaises(metrics_producer.MetricsProducerError):
            metrics_producer.validate_config(_cfg(dashboard_src="/tmp/dash"))

    def test_skip_while_alive_and_restart_after_exit(self):
        release = threading.Event()
        entered = threading.Event()

        class Blocking:
            def step(self, submit=False):
                entered.set()
                release.wait(timeout=30)
                return {"rate_limited": False, "posted": False, "disabled": False,
                        "errors": [], "submit": None}

        collector = Blocking()
        first = session_host.maybe_start_metrics_thread(None, collector)
        self.assertTrue(first.daemon)
        self.assertTrue(first.is_alive())
        self.assertIs(session_host.maybe_start_metrics_thread(first, collector), first)
        release.set()
        first.join(timeout=10)
        self.assertFalse(first.is_alive())
        second = session_host.maybe_start_metrics_thread(first, collector)
        self.assertIsNot(second, first)
        second.join(timeout=10)

    def test_rate_limited_and_disabled_rounds_stay_silent(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            session_host.metrics_step_once(SimpleNamespace(
                step=lambda submit=True: {"rate_limited": True}))
            session_host.metrics_step_once(SimpleNamespace(
                step=lambda submit=True: {"disabled": True, "rate_limited": False}))
        self.assertEqual(buf.getvalue(), "")

    def test_blocked_producer_does_not_delay_command_cycles(self):
        base = Path(tempfile.mkdtemp(prefix="metrics-run-", dir=str(REPO_TMP)))
        self.addCleanup(shutil.rmtree, str(base), True)
        journal = base / "journal"
        executed = []
        started = threading.Event()

        class BlockingProducer:
            def __init__(self, *a, **k):
                pass

            def step(self, submit=False):
                started.set()
                time.sleep(8)
                return {"rate_limited": False, "posted": False, "disabled": False,
                        "errors": [], "submit": None}

        class DummyQuota:
            last_collect_monotonic = 0
            last_hold = None
            last_error = None

            def __init__(self, *a, **k):
                pass

            def step(self):
                return None

        class DummyDispatcher:
            def __init__(self, *a, **k):
                pass

            def once(self):
                return []

        from agent_hub_host.status_runtime import StatusCollector

        class FastStatus(StatusCollector):
            def step(self, force=False, submit=False, prepared_sessions=None):
                return {"rate_limited": True, "observations": [], "skipped": [],
                        "errors": ["rate_limited"], "sender": None, "submit": None}

        with patch.object(session_host.config, "load_config",
                          return_value={"machine_id": "h", "access_token": "x"}), \
             patch.object(session_host.fleet, "load_settings",
                          return_value={"aplexer_bin": "fake", "cleanup_roots": []}), \
             patch.object(session_host, "qualify_sender_binding",
                          return_value=SimpleNamespace(session_id="ctrl", workspace="/tmp")), \
             patch.object(session_host.fleet, "Dispatcher", DummyDispatcher), \
             patch.object(session_host, "QuotaCollector", DummyQuota), \
             patch.object(session_host, "StatusCollector", FastStatus), \
             patch.object(session_host, "MetricsProducer", BlockingProducer), \
             patch.object(session_host.session_control, "execute",
                          side_effect=lambda *a, **k: executed.append(1)):
            begin = time.monotonic()
            session_host.run(str(base / "settings.json"), str(journal),
                             interval=1, fleet_interval=20, cycles=2)
            elapsed = time.monotonic() - begin
        self.assertEqual(len(executed), 2)
        self.assertLess(elapsed, 7,
                        "command loop waited for the blocked metrics producer")
        self.assertTrue(started.is_set())


if __name__ == "__main__":
    unittest.main()
