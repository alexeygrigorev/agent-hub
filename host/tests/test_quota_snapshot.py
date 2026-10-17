"""Targeted tests for quota_snapshot module and HostAgent quota integration.

Verifies:
- Schema 1.0 contract, strict allowlist, and data-driven safe windows.
- Strict percent non-bool, NaN, Inf, and range sanitization.
- Fail-closed error handling and secret/raw-error suppression.
- Honest absent/present 5h lifecycle (disappearing and returning).
- Monotonic 600s scheduling, wall rollback resilience, and failed collect cache.
- Config machine_id/token holds (no quse or post if missing).
- Bounded subprocess execution (shell=False, timeout<=30s, stdout limit).
- Network safety (no token leaks on 401/403/redirects, body <= 32 KiB).
- HostAgent integration (_run_ws and _poll_fallback regular ticks).
"""

import datetime
import json
import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import api, config, quota_snapshot


class MonotonicMockClock:
    def __init__(self, start_time: float = 1000.0, start_mono: float = 500.0):
        self._time = start_time
        self._mono = start_mono

    def time(self) -> float:
        return self._time

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float):
        self._time += seconds
        self._mono += seconds

    def rollback_wall_clock(self, seconds: float):
        """Simulate wall clock jump backwards while monotonic strictly advances."""
        self._time -= seconds


class TestQuotaSnapshotContract(unittest.TestCase):
    def test_create_snapshot_basic(self):
        raw = {
            "codex": {
                "status": "ok",
                "error": None,
                "observed_at": "2026-10-14T05:30:00Z",
                "details": {"limit_reached": False},
                "windows": {
                    "7d": {"percent_remaining": 54.0, "reset_at": "2026-10-21T00:00:00Z"}
                }
            }
        }
        snap = quota_snapshot.create_quota_snapshot(
            raw,
            host_id="mach-1",
            current_time=datetime.datetime(2026, 10, 14, 5, 31, 0, tzinfo=datetime.timezone.utc)
        )
        self.assertEqual(snap["schema_version"], "1.1")
        self.assertEqual(snap["collector_version"], quota_snapshot.COLLECTOR_VERSION)
        self.assertEqual(snap["host_id"], "mach-1")
        self.assertEqual(snap["status"], "ok")
        self.assertIsNone(snap["error"])
        self.assertIn("codex", snap["routes"])
        codex = snap["routes"]["codex"]
        self.assertEqual(codex["status"], "ok")
        self.assertIsNone(codex["error"])
        self.assertEqual(codex["observed_at"], "2026-10-14T05:30:00Z")
        self.assertEqual(codex["details"], {"limit_reached": False})
        self.assertTrue(codex["windows"]["7d"]["present"])
        self.assertEqual(codex["windows"]["7d"]["percent_remaining"], 54.0)

    def test_providers_strict_allowlist(self):
        raw = {
            "codex": {"status": "ok", "windows": {}},
            "unauthorized_provider": {"status": "ok", "windows": {}},
            "external_ai": {"status": "ok", "windows": {}}
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        self.assertIn("codex", snap["routes"])
        self.assertNotIn("unauthorized_provider", snap["routes"])
        self.assertNotIn("external_ai", snap["routes"])

    def test_details_has_grok_code_access_only_on_grok(self):
        raw = {
            "codex": {
                "status": "ok",
                "details": {"limit_reached": False, "has_grok_code_access": True}
            },
            "grok": {
                "status": "ok",
                "details": {"limit_reached": False, "has_grok_code_access": True}
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        self.assertNotIn("has_grok_code_access", snap["routes"]["codex"]["details"])
        self.assertTrue(snap["routes"]["grok"]["details"]["has_grok_code_access"])

    def test_sensitive_fields_stripped(self):
        raw = {
            "claude": {
                "status": "ok",
                "details": {
                    "limit_reached": False,
                    "banked_resets": [{"available": True}],
                    "subscription": "pro",
                    "prepaid_balance": 150.0,
                    "tokens": 1000000,
                    "account": "secret_acc"
                },
                "windows": {
                    "5h": {"percent_remaining": 90.0, "usage": 1234, "credits": 50}
                }
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        c_details = snap["routes"]["claude"]["details"]
        self.assertEqual(c_details, {"limit_reached": False})
        c_win = snap["routes"]["claude"]["windows"]["5h"]
        self.assertNotIn("usage", c_win)
        self.assertNotIn("credits", c_win)

    def test_data_driven_window_names_and_safe_regex(self):
        raw = {
            "zai": {
                "status": "ok",
                "windows": {
                    "valid_win-1": {"percent_remaining": 50.0},
                    "5h": {"percent_remaining": 80.0},
                    "invalid name with spaces": {"percent_remaining": 10.0},
                    "../malicious/path": {"percent_remaining": 20.0},
                    "-invalid_start": {"percent_remaining": 30.0}
                }
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        wins = snap["routes"]["zai"]["windows"]
        self.assertIn("valid_win-1", wins)
        self.assertIn("5h", wins)
        self.assertNotIn("invalid name with spaces", wins)
        self.assertNotIn("../malicious/path", wins)
        self.assertNotIn("-invalid_start", wins)

    def test_window_bound_32_max(self):
        raw_wins = {f"win_{i}": {"percent_remaining": 50.0} for i in range(50)}
        raw = {"go": {"status": "ok", "windows": raw_wins}}
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        wins = snap["routes"]["go"]["windows"]
        self.assertLessEqual(len(wins), 32)

    def test_absent_codex_5h_present_false_not_weekly_substitute(self):
        raw = {
            "codex": {
                "status": "ok",
                "windows": {
                    "5h": {"percent_remaining": None, "reset_at": None, "rolling": False},
                    "7d": {"percent_remaining": 60.0, "reset_at": "2026-10-20T00:00:00Z"}
                }
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        codex_wins = snap["routes"]["codex"]["windows"]
        self.assertFalse(codex_wins["5h"]["present"])
        self.assertIsNone(codex_wins["5h"]["percent_remaining"])
        self.assertIsNone(codex_wins["5h"]["reset_at"])
        # Ensure 7d is preserved as 7d without remapping to 5h
        self.assertTrue(codex_wins["7d"]["present"])
        self.assertEqual(codex_wins["7d"]["percent_remaining"], 60.0)

    def test_honest_5h_lifecycle_disappearing_and_returning(self):
        # Pass 1: 5h is available
        raw1 = {"codex": {"status": "ok", "windows": {"5h": {"percent_remaining": 45.0, "reset_at": "2026-10-14T10:00:00Z"}}}}
        snap1 = quota_snapshot.create_quota_snapshot(raw1, host_id="mach-1")
        self.assertTrue(snap1["routes"]["codex"]["windows"]["5h"]["present"])
        self.assertEqual(snap1["routes"]["codex"]["windows"]["5h"]["percent_remaining"], 45.0)

        # Pass 2: 5h disappears / is absent
        raw2 = {"codex": {"status": "ok", "windows": {"5h": {"percent_remaining": None, "reset_at": None}}}}
        snap2 = quota_snapshot.create_quota_snapshot(raw2, host_id="mach-1")
        self.assertFalse(snap2["routes"]["codex"]["windows"]["5h"]["present"])
        self.assertIsNone(snap2["routes"]["codex"]["windows"]["5h"]["percent_remaining"])

        # Pass 3: 5h returns honestly
        raw3 = {"codex": {"status": "ok", "windows": {"5h": {"percent_remaining": 88.0, "reset_at": "2026-10-14T15:00:00Z"}}}}
        snap3 = quota_snapshot.create_quota_snapshot(raw3, host_id="mach-1")
        self.assertTrue(snap3["routes"]["codex"]["windows"]["5h"]["present"])
        self.assertEqual(snap3["routes"]["codex"]["windows"]["5h"]["percent_remaining"], 88.0)


class TestQuotaSanitizationAndFailClosed(unittest.TestCase):
    def test_strict_percent_non_bool_nan_range(self):
        self.assertIsNone(quota_snapshot.sanitize_percent(True))
        self.assertIsNone(quota_snapshot.sanitize_percent(False))
        self.assertIsNone(quota_snapshot.sanitize_percent(float("nan")))
        self.assertIsNone(quota_snapshot.sanitize_percent(float("inf")))
        self.assertIsNone(quota_snapshot.sanitize_percent(float("-inf")))
        self.assertIsNone(quota_snapshot.sanitize_percent(-1.0))
        self.assertIsNone(quota_snapshot.sanitize_percent(100.01))
        self.assertEqual(quota_snapshot.sanitize_percent(0.0), 0.0)
        self.assertEqual(quota_snapshot.sanitize_percent(100.0), 100.0)
        self.assertEqual(quota_snapshot.sanitize_percent(42), 42.0)

    def test_raw_errors_replaced_with_unavailable(self):
        raw = {
            "copilot": {
                "status": "error",
                "error": "Secret internal backend trace: syntax error at char 12",
                "windows": {}
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        copilot = snap["routes"]["copilot"]
        self.assertEqual(copilot["status"], "error")
        self.assertEqual(copilot["error"], "unavailable")
        self.assertNotIn("Secret internal", json.dumps(snap))

    def test_native_stale_flag_marks_unavailable(self):
        raw = {
            "gemini": {
                "status": "ok",
                "stale": True,
                "windows": {"5h": {"percent_remaining": 50.0}}
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        self.assertEqual(snap["routes"]["gemini"]["status"], "error")
        self.assertEqual(snap["routes"]["gemini"]["error"], "unavailable")

    def test_observed_at_stale_and_future_boundary(self):
        current_time = datetime.datetime(2026, 10, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)

        # Observed 1300 seconds ago (>= 1200s boundary)
        stale_time = "2026-10-14T11:38:20Z"
        raw_stale = {"claude": {"status": "ok", "observed_at": stale_time, "windows": {}}}
        snap_stale = quota_snapshot.create_quota_snapshot(raw_stale, host_id="mach-1", current_time=current_time)
        self.assertEqual(snap_stale["routes"]["claude"]["status"], "error")
        self.assertEqual(snap_stale["routes"]["claude"]["error"], "unavailable")

        # Observed in the future (> 60s ahead)
        future_time = "2026-10-14T12:05:00Z"
        raw_future = {"claude": {"status": "ok", "observed_at": future_time, "windows": {}}}
        snap_future = quota_snapshot.create_quota_snapshot(raw_future, host_id="mach-1", current_time=current_time)
        self.assertEqual(snap_future["routes"]["claude"]["status"], "error")
        self.assertEqual(snap_future["routes"]["claude"]["error"], "unavailable")

        # Observed fresh (300s ago)
        fresh_time = "2026-10-14T11:55:00Z"
        raw_fresh = {"claude": {"status": "ok", "observed_at": fresh_time, "windows": {}}}
        snap_fresh = quota_snapshot.create_quota_snapshot(raw_fresh, host_id="mach-1", current_time=current_time)
        self.assertEqual(snap_fresh["routes"]["claude"]["status"], "ok")
        self.assertIsNone(snap_fresh["routes"]["claude"]["error"])

    def test_window_explicit_error_fails_closed(self):
        raw = {
            "grok": {
                "status": "ok",
                "windows": {
                    "weekly": {
                        "percent_remaining": None,
                        "reset_at": None,
                        "error": "rate limit error code 429"
                    }
                }
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        grok = snap["routes"]["grok"]
        self.assertEqual(grok["status"], "error")
        self.assertEqual(grok["error"], "unavailable")
        self.assertEqual(grok["windows"]["weekly"]["error"], "unavailable")
        self.assertNotIn("429", json.dumps(snap))

    def test_invalid_reset_at_format(self):
        raw = {
            "zai": {
                "status": "ok",
                "windows": {
                    "5h": {"percent_remaining": 50.0, "reset_at": "not-a-valid-date"}
                }
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        self.assertEqual(snap["routes"]["zai"]["status"], "error")
        self.assertEqual(snap["routes"]["zai"]["error"], "unavailable")
        self.assertIsNone(snap["routes"]["zai"]["windows"]["5h"]["reset_at"])


class TestQuotaSubprocessAndConfigHolds(unittest.TestCase):
    def test_config_missing_machine_id_holds(self):
        mock_runner = MagicMock()
        mock_http = MagicMock()
        clock = MonotonicMockClock()
        collector = quota_snapshot.QuotaCollector(
            clock=clock,
            runner=mock_runner,
            http_client=mock_http
        )

        with patch("agent_hub_host.config.load_config", return_value={"base_url": "https://hub", "access_token": "tok"}):
            res = collector.collect_and_post()
            self.assertIsNone(res)
            self.assertIn("missing machine_id", collector.last_hold)
            mock_runner.assert_not_called()
            mock_http.assert_not_called()

    def test_config_missing_token_holds(self):
        mock_runner = MagicMock()
        mock_http = MagicMock()
        clock = MonotonicMockClock()
        collector = quota_snapshot.QuotaCollector(
            clock=clock,
            runner=mock_runner,
            http_client=mock_http
        )

        with patch("agent_hub_host.config.load_config", return_value={"base_url": "https://hub", "machine_id": "m1"}):
            res = collector.collect_and_post()
            self.assertIsNone(res)
            self.assertIn("missing", collector.last_hold)
            mock_runner.assert_not_called()
            mock_http.assert_not_called()

    def test_execute_quse_bounded_stdout(self):
        # Exceeds max_bytes
        def oversized_runner(cmd, timeout, max_bytes):
            return b"{" + b"a" * (max_bytes + 10) + b"}"

        with self.assertRaises(ValueError):
            quota_snapshot.execute_quse(runner=oversized_runner)

    def test_execute_quse_timeout(self):
        def timeout_runner(cmd, timeout, max_bytes):
            raise TimeoutError("quse timed out")

        with self.assertRaises(TimeoutError):
            quota_snapshot.execute_quse(runner=timeout_runner)


class TestQuotaSchedulingAndCache(unittest.TestCase):
    def test_monotonic_scheduling_600s(self):
        mock_runner = MagicMock(return_value={"codex": {"status": "ok", "windows": {}}})
        mock_http = MagicMock()
        clock = MonotonicMockClock(start_time=1000.0, start_mono=500.0)
        collector = quota_snapshot.QuotaCollector(
            clock=clock,
            runner=mock_runner,
            http_client=mock_http
        )

        cfg = {"base_url": "https://hub", "machine_id": "m1", "access_token": "tok"}
        with patch("agent_hub_host.config.load_config", return_value=cfg):
            # First tick: runs collection
            res1 = collector.step()
            self.assertIsNotNone(res1)
            self.assertEqual(mock_runner.call_count, 1)

            # Advance by 300s (< 600s): step must do nothing
            clock.sleep(300.0)
            res2 = collector.step()
            self.assertIsNone(res2)
            self.assertEqual(mock_runner.call_count, 1)

            # Advance by another 301s (total 601s >= 600s): step runs
            clock.sleep(301.0)
            res3 = collector.step()
            self.assertIsNotNone(res3)
            self.assertEqual(mock_runner.call_count, 2)

    def test_failed_collect_cache_no_duplicate_quse(self):
        mock_runner = MagicMock(side_effect=RuntimeError("quse failure"))
        mock_http = MagicMock()
        clock = MonotonicMockClock(start_time=1000.0, start_mono=500.0)
        collector = quota_snapshot.QuotaCollector(
            clock=clock,
            runner=mock_runner,
            http_client=mock_http
        )

        cfg = {"base_url": "https://hub", "machine_id": "m1", "access_token": "tok"}
        with patch("agent_hub_host.config.load_config", return_value=cfg):
            # First step fails
            res1 = collector.step()
            self.assertIsNone(res1)
            self.assertEqual(mock_runner.call_count, 1)
            self.assertIn("RuntimeError", collector.last_error)

            # Next tick 1s later: MUST NOT run quse again!
            clock.sleep(1.0)
            res2 = collector.step()
            self.assertIsNone(res2)
            self.assertEqual(mock_runner.call_count, 1)

    def test_wall_clock_rollback_does_not_break_monotonic_schedule(self):
        mock_runner = MagicMock(return_value={"codex": {"status": "ok", "windows": {}}})
        mock_http = MagicMock()
        clock = MonotonicMockClock(start_time=10000.0, start_mono=500.0)
        collector = quota_snapshot.QuotaCollector(
            clock=clock,
            runner=mock_runner,
            http_client=mock_http
        )

        cfg = {"base_url": "https://hub", "machine_id": "m1", "access_token": "tok"}
        with patch("agent_hub_host.config.load_config", return_value=cfg):
            # Run at mono=500, wall=10000
            collector.step()
            self.assertEqual(mock_runner.call_count, 1)

            # Monotonic advances 100s, but wall clock jumps back 5000s!
            clock.sleep(100.0)
            clock.rollback_wall_clock(5000.0)

            # Still only 100s elapsed monotonically (< 600s) -> no call
            collector.step()
            self.assertEqual(mock_runner.call_count, 1)

            # Monotonic advances to 601s -> runs call
            clock.sleep(501.0)
            collector.step()
            self.assertEqual(mock_runner.call_count, 2)


class TestQuotaNetworkSafety(unittest.TestCase):
    @patch("agent_hub_host.api.safe_http_json")
    def test_post_quota_snapshot_success(self, mock_http):
        mock_http.return_value = {"status": "ok"}
        snap = {"schema_version": "1.0", "host_id": "m1", "status": "ok", "error": None, "routes": {}}
        res = quota_snapshot.post_quota_snapshot("https://hub", snap, token="secret_tok_xyz")
        self.assertEqual(res, {"status": "ok"})
        mock_http.assert_called_once()
        req = mock_http.call_args[0][0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer secret_tok_xyz")

    @patch("agent_hub_host.api.safe_http_json")
    def test_no_token_leaks_on_http_error(self, mock_http):
        import urllib.error
        mock_http.side_effect = urllib.error.HTTPError("https://hub/api/quota", 401, "Unauthorized", {}, None)
        snap = {"schema_version": "1.0", "host_id": "m1", "status": "ok", "error": None, "routes": {}}

        try:
            quota_snapshot.post_quota_snapshot("https://hub", snap, token="secret_token_12345")
            self.fail("Expected RuntimeError")
        except RuntimeError as e:
            self.assertNotIn("secret_token_12345", str(e))

    def test_oversized_payload_rejected_before_post(self):
        snap = {
            "schema_version": "1.0",
            "host_id": "m1",
            "status": "ok",
            "error": None,
            "routes": {
                "codex": {
                    "status": "ok",
                    "windows": {f"w_{i}": {"percent_remaining": 50.0, "blob": "x" * 2000} for i in range(20)}
                }
            }
        }
        with self.assertRaises(ValueError):
            quota_snapshot.post_quota_snapshot("https://hub", snap, token="tok")


class TestHostAgentQuotaIntegration(unittest.TestCase):
    def test_host_agent_ticks_quota_collector(self):
        mock_collector = MagicMock()
        clock = MonotonicMockClock()
        agent = api.HostAgent(clock=clock, quota_collector=mock_collector)

        # Check that step is called during _poll_fallback
        with patch("agent_hub_host.api.safe_http_json", return_value={"status": "ok"}), \
             patch("agent_hub_host.auth.get_access_token", return_value="tok"):
            agent.base_url = "https://hub"
            agent._poll_fallback()
            mock_collector.step.assert_called_once()

    def test_host_agent_ws_heartbeat_not_starved_and_no_reconnect_duplicate(self):
        clock = MonotonicMockClock(start_time=1000.0, start_mono=500.0)
        with patch("agent_hub_host.config.load_config", return_value={}):
            agent = api.HostAgent(clock=clock)
            self.assertIsNone(agent.base_url)
            self.assertIsNone(agent.quota_collector.base_url)

        def slow_runner(cmd, timeout, max_bytes):
            clock.sleep(30.0)
            return {"codex": {"status": "ok", "windows": {"7d": {"percent_remaining": 80.0}}}}

        posted = []
        agent.quota_collector.runner = slow_runner
        agent.quota_collector.http_client = lambda url, snap, token=None: posted.append((url, snap, token))

        cfg = {"base_url": "https://hub.example.com", "machine_id": "m1", "access_token": "tok"}
        sent_frames = []

        class FakeSock:
            def settimeout(self, t):
                pass
            def close(self):
                pass

        with patch("agent_hub_host.config.load_config", return_value=cfg), \
             patch("agent_hub_host.api.get_heartbeat_payload", return_value={"type": "heartbeat"}):
            # Advance clock 40s into WS session, then run _run_ws where slow_runner takes 30s (total 70s >= 60s)
            with patch("agent_hub_host.ws.WSConnection.send_frame", side_effect=lambda op, data: sent_frames.append((op, data))), \
                 patch("agent_hub_host.ws.WSConnection.recv_frame", side_effect=lambda: (True, 8, b"\x03\xe8")):
                # Set last_hb 40s before slow_runner by advancing clock inside _tick_quota
                orig_tick = agent._tick_quota
                def advance_and_tick():
                    clock.sleep(35.0)
                    orig_tick()
                with patch.object(agent, "_tick_quota", side_effect=advance_and_tick):
                    agent._run_ws(FakeSock(), b"")

        # Heartbeat frame (opcode 1) was sent immediately after slow_runner before Close frame (opcode 8)
        self.assertEqual([op for op, _ in sent_frames], [1, 8])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], "https://hub.example.com")


class TestReviewer195Regressions(unittest.TestCase):
    def test_real_popen_bounded_stdout_stderr_and_timeout_cleanup(self):
        from pathlib import Path
        tmp_dir = Path(__file__).resolve().parents[2] / ".local" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        fake_ok = tmp_dir / "test_quse_ok.py"
        fake_ok.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "assert sys.argv[1:] == ['--json']\n"
            "print(json.dumps({'codex': {'status': 'ok', 'windows': {'7d': {'percent_remaining': 75.0, 'reset_at': '2026-10-20T00:00:00Z'}}}}))\n"
        )
        fake_ok.chmod(0o755)

        fake_big = tmp_dir / "test_quse_big.py"
        fake_big.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.write('{' + '\"k\":1,' * 20000 + '\"z\":2}')\n"
            "sys.stdout.flush()\n"
        )
        fake_big.chmod(0o755)

        fake_slow = tmp_dir / "test_quse_slow.py"
        fake_slow.write_text(
            "#!/usr/bin/env python3\n"
            "import time\n"
            "time.sleep(5.0)\n"
        )
        fake_slow.chmod(0o755)

        try:
            res = quota_snapshot.execute_quse(quse_cmd=str(fake_ok), timeout=5.0)
            self.assertEqual(res["codex"]["windows"]["7d"]["percent_remaining"], 75.0)

            with self.assertRaises(ValueError):
                quota_snapshot.execute_quse(quse_cmd=str(fake_big), timeout=5.0, max_bytes=65536)

            with self.assertRaises(TimeoutError):
                quota_snapshot.execute_quse(quse_cmd=str(fake_slow), timeout=0.2)
        finally:
            fake_ok.unlink(missing_ok=True)
            fake_big.unlink(missing_ok=True)
            fake_slow.unlink(missing_ok=True)

    def test_runner_utf8_byte_cap_timeout_clamp_and_non_dict_rejected(self):
        multibyte_json = json.dumps({"codex": {"note": "\U0001f600" * 20000}}, ensure_ascii=False)
        self.assertLess(len(multibyte_json), quota_snapshot.MAX_RAW_STDOUT_BYTES)
        self.assertGreater(len(multibyte_json.encode("utf-8")), quota_snapshot.MAX_RAW_STDOUT_BYTES)

        with self.assertRaises(ValueError):
            quota_snapshot.execute_quse(runner=lambda *a, **kw: multibyte_json)

        with self.assertRaises(ValueError):
            quota_snapshot.execute_quse(runner=lambda *a, **kw: "[]")

        seen_kwargs = {}
        def check_clamp_runner(cmd, timeout, max_bytes):
            seen_kwargs["timeout"] = timeout
            seen_kwargs["max_bytes"] = max_bytes
            return {"codex": {"status": "ok", "windows": {}}}

        quota_snapshot.execute_quse(timeout=999.0, max_bytes=999999, runner=check_clamp_runner)
        self.assertEqual(seen_kwargs["timeout"], 30.0)
        self.assertEqual(seen_kwargs["max_bytes"], quota_snapshot.MAX_RAW_STDOUT_BYTES)

    def test_invalid_non_null_percent_fails_closed(self):
        now = datetime.datetime(2026, 10, 14, 6, 0, 0, tzinfo=datetime.timezone.utc)
        for bad_val in (-15.0, True, False, float("nan"), float("inf"), float("-inf"), 150.0, "bad"):
            raw = {
                "codex": {
                    "status": "ok",
                    "observed_at": "2026-10-14T05:59:00Z",
                    "windows": {
                        "5h": {"percent_remaining": bad_val, "reset_at": "2026-10-14T10:00:00Z"},
                    },
                }
            }
            snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1", current_time=now)
            codex = snap["routes"]["codex"]
            self.assertEqual(codex["status"], "error")
            self.assertEqual(codex["error"], "unavailable")
            self.assertFalse(codex["windows"]["5h"]["present"])
            self.assertIsNone(codex["windows"]["5h"]["percent_remaining"])
            self.assertIsNone(codex["windows"]["5h"]["reset_at"])
            self.assertEqual(codex["windows"]["5h"]["error"], "unavailable")

    def test_timezone_naive_and_date_only_timestamps_rejected_fail_closed(self):
        now = datetime.datetime(2026, 10, 14, 6, 0, 0, tzinfo=datetime.timezone.utc)
        raw = {
            "claude": {
                "status": "ok",
                "observed_at": "2026-10-14T05:55:00",
                "windows": {
                    "5h": {"percent_remaining": 80.0, "reset_at": "2026-10-15"}
                },
            }
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1", current_time=now)
        claude = snap["routes"]["claude"]
        self.assertEqual(claude["status"], "error")
        self.assertEqual(claude["error"], "unavailable")
        self.assertIsNone(claude["observed_at"])
        self.assertIsNone(claude["windows"]["5h"]["reset_at"])
        self.assertEqual(claude["windows"]["5h"]["error"], "unavailable")

        naive_now = datetime.datetime(2026, 10, 14, 6, 0, 0)
        with self.assertRaises(ValueError):
            quota_snapshot.create_quota_snapshot(raw, host_id="mach-1", current_time=naive_now)

    def test_falsy_non_null_observed_at_and_any_future_observed_at_rejected(self):
        now = datetime.datetime(2026, 10, 14, 6, 0, 0, tzinfo=datetime.timezone.utc)
        for bad_obs in (False, 0, "", [], {}, "2026-10-14T06:00:01Z", "2026-10-14T06:00:45Z"):
            raw = {
                "gemini": {
                    "status": "ok",
                    "observed_at": bad_obs,
                    "windows": {"5h": {"percent_remaining": 50.0, "reset_at": "2026-10-14T10:00:00Z"}},
                }
            }
            snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1", current_time=now)
            self.assertEqual(snap["routes"]["gemini"]["status"], "error")
            self.assertEqual(snap["routes"]["gemini"]["error"], "unavailable")
            self.assertIsNone(snap["routes"]["gemini"]["observed_at"])

    def test_malformed_provider_windows_details_invalid_names_and_overcap_fail_closed(self):
        raw = {
            "codex": "corrupted_provider_value",
            "claude": {"status": "ok", "windows": "not_a_dict", "details": "not_a_dict"},
            "gemini": {"status": "ok", "windows": {"bad name": {"percent_remaining": 50.0}}},
            "zai": {"status": "ok", "windows": {f"w_{i}": {"percent_remaining": 50.0} for i in range(35)}},
            "grok": {"status": "ok", "details": {"limit_reached": "no", "has_grok_code_access": 1}, "windows": {}},
        }
        snap = quota_snapshot.create_quota_snapshot(raw, host_id="mach-1")
        for prov in ("codex", "claude", "gemini", "zai", "grok"):
            self.assertIn(prov, snap["routes"])
            self.assertEqual(snap["routes"][prov]["status"], "error")
            self.assertEqual(snap["routes"][prov]["error"], "unavailable")

    def test_wrong_origin_and_invalid_config_blocked_before_quse_and_token_use(self):
        clock = MonotonicMockClock()
        mock_runner = MagicMock(return_value={"codex": {"status": "ok", "windows": {}}})
        posted = []
        collector = quota_snapshot.QuotaCollector(
            clock=clock,
            base_url="https://wrong-origin.example.com",
            runner=mock_runner,
            http_client=lambda url, snap, token=None: posted.append((url, snap, token)),
        )
        cfg = {
            "base_url": "https://hub.example.com",
            "machine_id": "mach-1",
            "access_token": "secret_host_token",
        }
        with patch("agent_hub_host.config.load_config", return_value=cfg):
            res = collector.collect_and_post()
            self.assertIsNone(res)
            self.assertEqual(mock_runner.call_count, 0)
            self.assertEqual(len(posted), 0)
            self.assertIn("origin mismatch", collector.last_hold)

            snap = quota_snapshot.create_quota_snapshot({"codex": {"status": "ok", "windows": {}}}, host_id="mach-1")
            with patch("agent_hub_host.api.safe_http_json") as mock_safe:
                with self.assertRaises(quota_snapshot.QuotaHold):
                    quota_snapshot.post_quota_snapshot("https://attacker.example.com", snap, token=None)
                with self.assertRaises(quota_snapshot.QuotaHold):
                    quota_snapshot.get_quota_snapshot("https://attacker.example.com", token=None)
                mock_safe.assert_not_called()

        collector_default = quota_snapshot.QuotaCollector(clock=clock, runner=mock_runner)
        for bad_cfg in (
            {"base_url": "https://hub.example.com", "machine_id": "   ", "access_token": "tok"},
            {"base_url": "http://insecure.example.com", "machine_id": "m1", "access_token": "tok"},
            {"base_url": "https://hub.example.com", "machine_id": 123, "access_token": "tok"},
            {"base_url": "https://hub.example.com", "machine_id": "m1", "access_token": "   "},
        ):
            with patch("agent_hub_host.config.load_config", return_value=bad_cfg):
                res = collector_default.collect_and_post()
                self.assertIsNone(res)
                self.assertEqual(mock_runner.call_count, 0)


class TestHubAbsentWindowConsistency(unittest.TestCase):
    """Hub rule: present:false (absent or errored) carries percent_remaining=null AND reset_at=null."""

    NOW = datetime.datetime(2026, 10, 14, 12, 0, 30, tzinfo=datetime.timezone.utc)

    def _codex(self, windows, limit_reached=False):
        raw = {"codex": {"status": "ok", "error": None, "details": {"limit_reached": limit_reached}, "windows": windows}}
        return quota_snapshot.create_quota_snapshot(raw, host_id="mach-1", current_time=self.NOW)["routes"]["codex"]

    def assertAbsentErrored(self, win):
        self.assertEqual(
            win,
            {"percent_remaining": None, "reset_at": None, "present": False, "rolling": win["rolling"], "error": "unavailable"},
        )

    def test_case_c_invalid_percent_with_valid_reset_clears_reset(self):
        codex = self._codex({
            "5h": {"percent_remaining": 150, "reset_at": "2026-10-14T15:00:00Z", "rolling": True},
            "7d": {"percent_remaining": 70, "reset_at": "2026-10-20T00:00:00Z"},
        })
        self.assertEqual(codex["status"], "error")
        self.assertEqual(codex["error"], "unavailable")
        self.assertAbsentErrored(codex["windows"]["5h"])
        self.assertTrue(codex["windows"]["5h"]["rolling"])
        # 7d stays 7d, never aliased into 5h
        self.assertEqual(codex["windows"]["7d"]["percent_remaining"], 70.0)
        self.assertEqual(codex["windows"]["7d"]["reset_at"], "2026-10-20T00:00:00Z")
        self.assertTrue(codex["windows"]["7d"]["present"])

    def test_case_d_null_percent_with_reset_clears_reset(self):
        codex = self._codex({
            "5h": {"percent_remaining": None, "reset_at": "2026-10-14T15:00:00Z", "rolling": True},
            "7d": {"percent_remaining": 0, "reset_at": "2026-10-20T00:00:00Z"},
        }, limit_reached=True)
        self.assertEqual(codex["status"], "error")
        self.assertAbsentErrored(codex["windows"]["5h"])
        self.assertEqual(codex["details"], {"limit_reached": True})
        self.assertEqual(codex["windows"]["7d"]["percent_remaining"], 0.0)

    def test_case_h_explicit_present_false_with_percent_fails_closed(self):
        for win in (
            {"percent_remaining": 10, "reset_at": None, "present": False, "rolling": True},
            {"percent_remaining": 10, "reset_at": "2026-10-14T15:00:00Z", "present": False},
            {"percent_remaining": 0, "reset_at": None, "present": False},
        ):
            codex = self._codex({"5h": win, "7d": {"percent_remaining": 50, "reset_at": "2026-10-20T00:00:00Z"}})
            self.assertEqual(codex["status"], "error")
            self.assertEqual(codex["error"], "unavailable")
            self.assertAbsentErrored(codex["windows"]["5h"])
            self.assertEqual(codex["windows"]["7d"]["percent_remaining"], 50.0)

    def test_explicit_window_error_with_values_clears_percent_and_reset(self):
        codex = self._codex({"7d": {"percent_remaining": 40, "reset_at": "2026-10-20T00:00:00Z", "error": "boom"}})
        self.assertEqual(codex["status"], "error")
        self.assertAbsentErrored(codex["windows"]["7d"])
        self.assertNotIn("boom", json.dumps(codex))

    def test_controls_absent_and_present_windows_unchanged(self):
        codex = self._codex({
            "5h": {"percent_remaining": None, "reset_at": None, "rolling": False},
            "7d": {"percent_remaining": 78.5, "reset_at": "2026-10-20T00:00:00+00:00", "present": True},
            "monthly": {"percent_remaining": None, "reset_at": None, "present": False},
        })
        self.assertEqual(codex["status"], "ok")
        self.assertIsNone(codex["error"])
        self.assertEqual(codex["windows"]["5h"], {"percent_remaining": None, "reset_at": None, "present": False, "rolling": False, "error": None})
        self.assertEqual(codex["windows"]["monthly"], {"percent_remaining": None, "reset_at": None, "present": False, "rolling": False, "error": None})
        self.assertEqual(codex["windows"]["7d"], {"percent_remaining": 78.5, "reset_at": "2026-10-20T00:00:00Z", "present": True, "rolling": False, "error": None})

    def test_every_emitted_window_satisfies_hub_absent_rule(self):
        pcts = (None, 0, 55.5, 100, 150, -1, True, float("nan"), "x")
        resets = (None, "2026-10-14T15:00:00Z", "2026-10-15", "bad")
        presents = ("missing", None, True, False, "yes")
        errors = ("missing", None, "e")
        for pct in pcts:
            for reset in resets:
                for present in presents:
                    for err in errors:
                        win = {"percent_remaining": pct, "reset_at": reset}
                        if present != "missing":
                            win["present"] = present
                        if err != "missing":
                            win["error"] = err
                        out, has_error = quota_snapshot.sanitize_window("5h", win, self.NOW)
                        with self.subTest(win=win):
                            self.assertIs(type(out["present"]), bool)
                            if not out["present"]:
                                self.assertIsNone(out["percent_remaining"])
                                self.assertIsNone(out["reset_at"])
                            else:
                                self.assertIsInstance(out["percent_remaining"], float)
                                self.assertFalse(has_error)
                            if has_error:
                                self.assertFalse(out["present"])
                                self.assertEqual(out["error"], "unavailable")
                            if present is False and pct is not None:
                                self.assertTrue(has_error)


if __name__ == "__main__":
    unittest.main()

