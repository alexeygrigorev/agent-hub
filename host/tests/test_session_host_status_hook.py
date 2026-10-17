"""session_host status hook: single-inflight observer never blocks commands.

Synthetic only: no live sessions, no network, no Hub. Uses the REAL
StatusCollector for the missing-allowlist case (fails closed, no HTTP) and
fakes for the loop-timing case. Proves a deliberately blocked collector does
not prevent queued command cycles, and that quota/command paths are untouched.
"""

import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host import session_host
from agent_hub_host.status_runtime import StatusCollector

REPO_TMP = Path(__file__).resolve().parents[2] / ".local" / "tmp"


class TestStatusHook(unittest.TestCase):
    def test_missing_allowlist_fails_closed_without_http(self):
        journal = Path(tempfile.mkdtemp(prefix="status-hook-", dir=str(REPO_TMP)))
        self.addCleanup(shutil.rmtree, str(journal), True)
        submitted = []
        collector = StatusCollector(
            allowlist_path=str(journal / "status-allowlist.json"),
            machine_id="h", min_interval_s=60,
            submit_fn=lambda obs, base, token: submitted.append(True))
        result = collector.step(submit=True)
        self.assertFalse(result["rate_limited"])
        self.assertTrue(result["errors"])
        self.assertEqual(result["observations"], [])
        self.assertEqual(submitted, [])

    def test_skip_while_alive_and_restart_after_exit(self):
        release = threading.Event()
        entered = threading.Event()

        class Blocking:
            def step(self, submit=False):
                entered.set()
                release.wait(timeout=30)
                return {"rate_limited": False, "observations": [],
                        "skipped": [], "errors": []}

        collector = Blocking()
        first = session_host.maybe_start_status_thread(None, collector)
        self.assertTrue(first.daemon)
        self.assertTrue(first.is_alive())
        # Second start while alive returns the SAME thread (skip, no second).
        self.assertIs(session_host.maybe_start_status_thread(first, collector), first)
        self.assertEqual(threading.active_count() >= 2, True)
        release.set()
        first.join(timeout=10)
        self.assertFalse(first.is_alive())
        second = session_host.maybe_start_status_thread(first, collector)
        self.assertIsNot(second, first)
        second.join(timeout=10)

    def test_rate_limited_round_stays_silent(self):
        class Limited:
            def step(self, submit=False):
                return {"rate_limited": True, "observations": [],
                        "skipped": [], "errors": ["rate_limited"]}

        buf = io.StringIO()
        with redirect_stdout(buf):
            session_host.status_step_once(Limited())
        self.assertEqual(buf.getvalue(), "")

    def test_blocked_collector_does_not_delay_command_cycles(self):
        base = Path(tempfile.mkdtemp(prefix="status-run-", dir=str(REPO_TMP)))
        self.addCleanup(shutil.rmtree, str(base), True)
        journal = base / "journal"
        executed = []
        started = threading.Event()

        class BlockingCollector:
            def __init__(self, *a, **k):
                pass

            def step(self, submit=False):
                started.set()
                time.sleep(8)
                return {"rate_limited": False, "observations": [],
                        "skipped": [], "errors": []}

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

        with patch.object(session_host.config, "load_config",
                          return_value={"machine_id": "h", "access_token": "x"}), \
             patch.object(session_host.fleet, "load_settings",
                          return_value={"aplexer_bin": "fake", "cleanup_roots": []}), \
             patch.object(session_host, "qualify_sender_binding",
                          return_value=SimpleNamespace(session_id="ctrl", workspace="/tmp")), \
             patch.object(session_host.fleet, "Dispatcher", DummyDispatcher), \
             patch.object(session_host, "QuotaCollector", DummyQuota), \
             patch.object(session_host, "StatusCollector", BlockingCollector), \
             patch.object(session_host.session_control, "execute",
                          side_effect=lambda *a, **k: executed.append(1)):
            begin = time.monotonic()
            session_host.run(str(base / "settings.json"), str(journal),
                             interval=1, fleet_interval=20, cycles=2)
            elapsed = time.monotonic() - begin
        # Two full command cycles ran while the observer was still blocked.
        self.assertEqual(len(executed), 2)
        self.assertLess(elapsed, 7,
                        "command loop waited for the blocked status observer")
        self.assertTrue(started.is_set())


if __name__ == "__main__":
    unittest.main()
