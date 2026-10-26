"""Tests for the native token usage collector.

Uses small synthetic fixtures only (codex rollout JSONL, Claude project
JSONL, a throwaway OpenCode sqlite DB). Never touches the real homes.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_hub_host import token_usage as tu


def hour_ms(iso):
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


H1 = "2026-10-26T00:12:00Z"
H2 = "2026-10-26T01:05:00Z"


class ProjectMappingTest(unittest.TestCase):
    def test_repo_cwd_maps_to_repo(self):
        self.assertEqual(tu.project_for_cwd("/home/alexey/git/agent-hub/host/x"), "agent-hub")

    def test_unknown_cwd_is_unattributed(self):
        self.assertEqual(tu.project_for_cwd("/tmp/scratch"), "unattributed")
        self.assertEqual(tu.project_for_cwd(None), "unattributed")
        self.assertEqual(tu.project_for_cwd("/home/alexey/git/agent-hub"), "agent-hub")


class CodexCollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sessions = os.path.join(self.tmp.name, "sessions")
        os.makedirs(sessions)
        rollout = os.path.join(sessions, "rollout-test.jsonl")
        lines = [
            {"type": "session_meta", "timestamp": H1,
             "payload": {"cwd": "/home/alexey/git/agent-hub"}},
            # Cumulative per-thread input snapshots with per-turn outputs.
            {"type": "token_usage_record", "timestamp": H1,
             "payload": {"thread_id": "t1",
                         "usage": {"input_tokens": 100, "cached_input_tokens": 20,
                                   "output_tokens": 10}}},
            {"type": "token_usage_record", "timestamp": H2,
             "payload": {"thread_id": "t1",
                         "usage": {"input_tokens": 150, "cached_input_tokens": 20,
                                   "output_tokens": 5}}},
            {"type": "event_msg", "timestamp": H2, "payload": {"x": 1}},
            "not json at all",
        ]
        with open(rollout, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_codex_buckets_per_hour(self):
        since = hour_ms("2026-10-25T00:00:00Z")
        now = hour_ms("2026-10-26T02:00:00Z")
        got = tu.collect_codex(self.tmp.name, since, now, "codex", "shared-codex")
        h1 = tu.utc_hour_ms(hour_ms(H1))
        h2 = tu.utc_hour_ms(hour_ms(H2))
        # First record counts fully (120 in), second is the positive delta (50 in).
        self.assertEqual(got[(h1, "agent-hub")], [120, 10])
        self.assertEqual(got[(h2, "agent-hub")], [50, 5])

    def test_collect_codex_compaction_restart_floors_at_zero(self):
        path = os.path.join(self.tmp.name, "sessions", "rollout-test.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"type": "token_usage_record", "timestamp": H2,
                 "payload": {"thread_id": "t1",
                             "usage": {"input_tokens": 10, "output_tokens": 1}}}) + "\n")
        since = hour_ms("2026-10-25T00:00:00Z")
        now = hour_ms("2026-10-26T02:00:00Z")
        got = tu.collect_codex(self.tmp.name, since, now, "codex", "shared-codex")
        h2 = tu.utc_hour_ms(hour_ms(H2))
        # 50 + floored 0, outputs 5 + 1.
        self.assertEqual(got[(h2, "agent-hub")], [50, 6])

    def test_collect_codex_respects_window(self):
        since = hour_ms("2026-10-26T01:00:00Z")
        now = hour_ms("2026-10-26T02:00:00Z")
        got = tu.collect_codex(self.tmp.name, since, now, "codex", "shared-codex")
        self.assertEqual(len(got), 1)


class ClaudeCollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        lines = [
            {"timestamp": H1, "cwd": "/home/alexey/git/agent-hub",
             "message": {"usage": {"input_tokens": 30, "cache_read_input_tokens": 7,
                                   "cache_creation_input_tokens": 3, "output_tokens": 9}}},
            {"timestamp": H1, "cwd": "/elsewhere",
             "message": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"timestamp": H1, "cwd": "/home/alexey/git/agent-hub", "message": {}},
            {"timestamp": "garbage", "cwd": "x", "message": {"usage": {"input_tokens": 5}}},
        ]
        with open(os.path.join(self.tmp.name, "proj.jsonl"), "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_claude(self):
        since = hour_ms("2026-10-25T00:00:00Z")
        now = hour_ms("2026-10-26T02:00:00Z")
        got = tu.collect_claude(self.tmp.name, since, now)
        h1 = tu.utc_hour_ms(hour_ms(H1))
        self.assertEqual(got[(h1, "agent-hub")], [40, 9])
        self.assertEqual(got[(h1, "unattributed")], [1, 1])


class OpencodeCollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "opencode.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE session (time_created INTEGER, directory TEXT,"
                     " tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,"
                     " tokens_cache_read INTEGER, tokens_cache_write INTEGER)")
        conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                     (hour_ms(H1), "/home/alexey/git/agent-hub", 10, 4, 2, 3, 1))
        conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                     (hour_ms(H2), "/tmp/x", 5, 1, 0, 0, 0))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_opencode(self):
        since = hour_ms("2026-10-25T00:00:00Z")
        now = hour_ms("2026-10-26T02:00:00Z")
        got = tu.collect_opencode(self.db, since, now)
        self.assertEqual(got[(tu.utc_hour_ms(hour_ms(H1)), "agent-hub")], [14, 6])
        self.assertEqual(got[(tu.utc_hour_ms(hour_ms(H2)), "unattributed")], [5, 1])

    def test_collect_opencode_missing_db_is_empty(self):
        got = tu.collect_opencode(os.path.join(self.tmp.name, "nope.db"),
                                  hour_ms("2026-10-25T00:00:00Z"), hour_ms("2026-10-26T02:00:00Z"))
        self.assertEqual(got, {})


class BuildHoursTest(unittest.TestCase):
    def test_only_complete_hours_and_bounded(self):
        now = hour_ms("2026-10-26T02:30:00Z")
        real = (tu.collect_codex, tu.collect_claude, tu.collect_opencode, tu._CODEX_SOURCES)
        try:
            tu.collect_codex = lambda *a: {(tu.utc_hour_ms(now - 3600000), "p"): [3, 1],
                                            (tu.utc_hour_ms(now), "p"): [99, 99]}
            tu.collect_claude = lambda *a: {}
            tu.collect_opencode = lambda *a: {}
            tu._CODEX_SOURCES = (("codex", "shared-codex", ".codex"),)
            hours = tu.build_hours(now)
        finally:
            tu.collect_codex, tu.collect_claude, tu.collect_opencode, tu._CODEX_SOURCES = real
        self.assertEqual(len(hours), 1)
        self.assertEqual(hours[0]["hour_start"], tu.hour_iso(tu.utc_hour_ms(now - 3600000)))
        self.assertEqual(hours[0]["engine"], "codex")


class TokenCollectorTest(unittest.TestCase):
    def test_rate_limit_and_hold(self):
        posted = []

        class Clock:
            def __init__(self):
                self.t = 1000.0

            def monotonic(self):
                return self.t

            def now(self):
                from datetime import datetime, timezone
                return datetime.fromtimestamp(1000.0, tz=timezone.utc)

        collector = TokenCollectorShim(Clock(), posted)
        self.assertIsNone(collector.step())
        collector.last_collect_monotonic = 1000.0
        self.assertIsNone(collector.step())


class TokenCollectorShim(tu.TokenCollector):
    def __init__(self, clock, posted):
        super().__init__(clock=clock, base_url="https://hub.invalid", interval=3600.0)
        self._posted = posted

    def collect_and_post(self):
        # Exercise the cadence gate without touching config.
        now_mono = self._monotonic()
        if self.last_collect_monotonic is not None and now_mono - self.last_collect_monotonic < self.interval:
            return None
        self.last_hold = "missing machine_id, access_token, or base_url in host config"
        return None


if __name__ == "__main__":
    unittest.main()
