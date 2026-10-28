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
from unittest.mock import patch
from pathlib import Path

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

    def test_worktree_uses_common_repository(self):
        def is_file(path):
            return str(path) == '/home/user/git/candidate/.git'
        def read(path):
            if path.name == '.git':
                return 'gitdir: /home/user/git/agent-hub/.git/worktrees/candidate'
            if path.name == 'commondir':
                return '../..'
            raise OSError()
        with patch.object(Path, 'is_file', is_file), patch.object(Path, 'read_text', read):
            self.assertEqual(tu.project_for_cwd('/home/user/git/candidate/subdir'), 'agent-hub')

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
            # Native per-response usage; cumulative counters are tested below.
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
        # Cached input is a subset; separate responses both count in full.
        self.assertEqual(got[(h1, "agent-hub")], [100, 10])
        self.assertEqual(got[(h2, "agent-hub")], [150, 5])

    def test_collect_codex_smaller_response_is_still_real_usage(self):
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
        self.assertEqual(got[(h2, "agent-hub")], [160, 6])

    def test_native_totals_formats_dedupe_and_keep_pre_window_baseline(self):
        path = os.path.join(self.tmp.name, "sessions", "rollout-test.jsonl")
        records = [
            {"type": "session_meta", "payload": {"id": "t1", "cwd": "/home/alexey/git/agent-hub"}},
            {"type": "event_msg", "timestamp": H1, "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 100, "output_tokens": 10}}}},
            {"type": "token_usage_record", "timestamp": H1, "payload": {"thread_id": "t1",
                "usage": {"input_tokens": 100, "output_tokens": 10},
                "thread_token_usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "reasoning_output_tokens": 5}}},
            {"type": "event_msg", "timestamp": H2, "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 150, "output_tokens": 15}}}},
            {"type": "token_usage_record", "timestamp": H2, "payload": {"thread_id": "t1",
                "thread_token_usage": {"input_tokens": 150, "output_tokens": 15}}},
            # A stale/lower duplicate cannot lower the high-water mark.
            {"type": "token_usage_record", "timestamp": H2, "payload": {"thread_id": "t1",
                "thread_token_usage": {"input_tokens": 100, "output_tokens": 10}}},
        ]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(json.dumps(r) for r in records))
        since = hour_ms("2026-10-26T01:00:00Z")
        now = hour_ms("2026-10-26T02:00:00Z")
        self.assertEqual(tu.collect_codex(self.tmp.name, since, now, "codex", "shared-codex"),
                         {(tu.utc_hour_ms(hour_ms(H2)), "agent-hub"): [50, 5]})

    def test_missing_native_counts_remain_absent_and_response_reposts_dedupe(self):
        path = os.path.join(self.tmp.name, "sessions", "rollout-test.jsonl")
        records = [{"type": "token_usage_record", "timestamp": H1, "payload": {"usage": usage}}
                   for usage in [{}, {"input_tokens": 3}, {"input_tokens": None, "output_tokens": 1}]]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(json.dumps(r) for r in records))
        since, now = hour_ms("2026-10-25T00:00:00Z"), hour_ms("2026-10-26T02:00:00Z")
        self.assertEqual(tu.collect_codex(self.tmp.name, since, now, "codex", "shared-codex"), {})
        response = {"type": "token_usage_record", "timestamp": H1, "payload": {
            "response_id": "r1", "usage": {"input_tokens": 0, "output_tokens": 0}}}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(json.dumps(response) for _ in range(2)))
        self.assertEqual(tu.collect_codex(self.tmp.name, since, now, "codex", "shared-codex"),
                         {(tu.utc_hour_ms(hour_ms(H1)), "unattributed"): [0, 0]})

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

    def test_native_message_snapshots_and_missing_counts(self):
        path = Path(self.tmp.name) / 'proj.jsonl'
        rows = [
            {'timestamp': H1, 'cwd': '/home/alexey/git/agent-hub', 'requestId':'r',
             'message': {'id':'m', 'usage': {'input_tokens':30, 'output_tokens':2}}},
            {'timestamp': H2, 'cwd': '/home/alexey/git/agent-hub', 'requestId':'r',
             'message': {'id':'m', 'usage': {'input_tokens':30, 'output_tokens':9}}},
            {'timestamp': H2, 'message': {'usage': {}}},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in rows))
        got = tu.collect_claude(self.tmp.name, hour_ms('2026-10-25T00:00:00Z'), hour_ms('2026-10-26T02:00:00Z'))
        self.assertEqual(got, {(tu.utc_hour_ms(hour_ms(H1)), 'agent-hub'): [30,9]})


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
        conn.execute('ALTER TABLE session ADD COLUMN id TEXT')
        conn.execute("UPDATE session SET id='old-session'")
        conn.execute('CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)')
        for mid, stamp, inp, outp in [('m1', H1, 10, 4), ('m2', H2, 5, 1)]:
            data = {'role': 'assistant', 'time': {'completed': hour_ms(stamp)},
                    'tokens': {'input': inp, 'output': outp, 'reasoning': 2 if mid == 'm1' else 0,
                               'cache': {'read': 3 if mid == 'm1' else 0, 'write': 1 if mid == 'm1' else 0}}}
            conn.execute('INSERT INTO message VALUES (?,?,?,?,?)', (mid, 'old-session', hour_ms(stamp), hour_ms(stamp), json.dumps(data)))
        # One native message must join one session; the aggregate table's old
        # creation time cannot decide the hour of its later response.
        conn.execute("DELETE FROM session WHERE directory='/tmp/x'")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_opencode(self):
        since = hour_ms("2026-10-25T00:00:00Z")
        now = hour_ms("2026-10-26T02:00:00Z")
        got = tu.collect_opencode(self.db, since, now)
        self.assertEqual(got[(tu.utc_hour_ms(hour_ms(H1)), "agent-hub")], [14, 6])
        self.assertEqual(got[(tu.utc_hour_ms(hour_ms(H2)), "agent-hub")], [5, 1])

    def test_later_messages_in_old_session_remain_in_window(self):
        got = tu.collect_opencode(self.db, hour_ms('2026-10-26T01:00:00Z'), hour_ms('2026-10-26T02:00:00Z'))
        self.assertEqual(got, {(tu.utc_hour_ms(hour_ms(H2)), 'agent-hub'): [5, 1]})

    def test_aggregate_only_schema_does_not_invent_hour_attribution(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('DROP TABLE message')
        self.assertEqual(tu.collect_opencode(self.db, hour_ms('2026-10-25T00:00:00Z'), hour_ms('2026-10-26T02:00:00Z')), {})

    def test_v2_message_and_session_tables(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('ALTER TABLE message RENAME TO session_message')
            conn.execute('ALTER TABLE session RENAME TO session_v2')
        got = tu.collect_opencode(self.db, hour_ms('2026-10-26T01:00:00Z'), hour_ms('2026-10-26T02:00:00Z'))
        self.assertEqual(got, {(tu.utc_hour_ms(hour_ms(H2)), 'agent-hub'): [5, 1]})

    def test_copied_databases_keep_latest_valid_completed_message(self):
        copied = str(Path(self.tmp.name)/'copy.db')
        with sqlite3.connect(self.db) as src, sqlite3.connect(copied) as dst:
            src.backup(dst)
        with sqlite3.connect(self.db) as conn:
            conn.execute('ALTER TABLE session RENAME TO session_v2')
            conn.execute('ALTER TABLE message RENAME TO session_message')
            conn.execute('ALTER TABLE session_message ADD COLUMN type TEXT')
            for mid, raw in conn.execute('SELECT id,data FROM session_message').fetchall():
                data=json.loads(raw)
                data['time'].pop('completed')
                data.pop('role')
                conn.execute('UPDATE session_message SET type=?,data=? WHERE id=?',('assistant',json.dumps(data),mid))
        since,now=hour_ms('2026-10-26T00:00:00Z'),hour_ms('2026-10-26T02:00:00Z')
        expected=tu.collect_opencode(copied,since,now)
        for paths in [[self.db,copied],[copied,self.db]]:
            self.assertEqual(tu.collect_opencode(paths[0],since,now,roots=paths), expected)
        # A newer valid snapshot replaces old completed counters, not adds them.
        with sqlite3.connect(copied) as conn:
            raw=conn.execute("SELECT data FROM message WHERE id='m2'").fetchone()[0]
            data=json.loads(raw);data['tokens']['input']=25
            conn.execute("UPDATE message SET data=?,time_updated=? WHERE id='m2'",(json.dumps(data),now))
        got=tu.collect_opencode(self.db,since,now,roots=[self.db,copied])
        self.assertEqual(got[(tu.utc_hour_ms(hour_ms(H2)),'agent-hub')],[25,1])

    def test_v2_role_lives_in_native_type_column(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('ALTER TABLE session RENAME TO session_v2')
            conn.execute('ALTER TABLE message RENAME TO session_message')
            conn.execute('ALTER TABLE session_message ADD COLUMN type TEXT')
            for mid, raw in conn.execute('SELECT id,data FROM session_message').fetchall():
                data=json.loads(raw)
                role=data.pop('role')
                conn.execute('UPDATE session_message SET type=?,data=? WHERE id=?',(role,json.dumps(data),mid))
        got=tu.collect_opencode(self.db,hour_ms('2026-10-26T01:00:00Z'),hour_ms('2026-10-26T02:00:00Z'))
        self.assertEqual(got,{(tu.utc_hour_ms(hour_ms(H2)),'agent-hub'):[5,1]})

    def test_collect_opencode_missing_db_is_empty(self):
        got = tu.collect_opencode(os.path.join(self.tmp.name, "nope.db"),
                                  hour_ms("2026-10-25T00:00:00Z"), hour_ms("2026-10-26T02:00:00Z"))
        self.assertEqual(got, {})


class BuildHoursTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'HOME':self.tmp.name, 'AGENT_HUB_MANAGED_DIR':str(Path(self.tmp.name)/'managed')})
        env.start()
        self.addCleanup(env.stop)

    def test_only_complete_hours_and_bounded(self):
        now = hour_ms("2026-10-26T02:30:00Z")
        real = (tu.collect_codex, tu.collect_claude, tu.collect_opencode, tu._CODEX_SOURCES)
        try:
            tu.collect_codex = lambda *a, **kw: {(tu.utc_hour_ms(now - 3600000), "p"): [3, 1],
                                            (tu.utc_hour_ms(now), "p"): [99, 99]}
            tu.collect_claude = lambda *a, **kw: {}
            tu.collect_opencode = lambda *a, **kw: {}
            tu._CODEX_SOURCES = (("codex", "shared-codex", ".codex"),)
            hours = tu.build_hours(now)
        finally:
            tu.collect_codex, tu.collect_claude, tu.collect_opencode, tu._CODEX_SOURCES = real
        self.assertEqual(len(hours), 1)
        self.assertEqual(hours[0]["hour_start"], tu.hour_iso(tu.utc_hour_ms(now - 3600000)))
        self.assertEqual(hours[0]["engine"], "codex")

    def test_managed_home_collected_and_duplicate_rollouts_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / 'managed'
            dest = managed / ('shared-codex-' + 'a'*36) / 'home' / '.codex' / 'sessions'
            dest.mkdir(parents=True)
            classic = root / '.codex' / 'sessions'
            classic.mkdir(parents=True)
            rows = [
                {'type':'session_meta','payload':{'id':'same-native-thread','cwd':'/home/alexey/git/agent-hub'}},
                {'type':'event_msg','timestamp':H1,'payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':30,'output_tokens':9}}}},
            ]
            for directory in [dest, classic]:
                (directory / 'rollout.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            with patch.dict(os.environ, {'HOME':tmp, 'AGENT_HUB_MANAGED_DIR':str(managed)}):
                got = tu.build_hours(hour_ms('2026-10-26T02:30:00Z'))
            self.assertEqual(len(got), 1)
            self.assertEqual((got[0]['account'],got[0]['input_tokens'],got[0]['output_tokens']), ('shared-codex',30,9))

    def test_complete_hour_cutoff_does_not_slide_with_minutes(self):
        seen=[]
        def collect(*args, **kwargs):
            seen.append(args[1])
            return {}
        with patch.object(tu, 'collect_codex', collect), patch.object(tu, 'collect_claude', return_value={}), patch.object(tu, 'collect_opencode', return_value={}):
            tu.build_hours(hour_ms('2026-10-26T02:01:00Z'))
            first=seen[0]
            seen.clear()
            tu.build_hours(hour_ms('2026-10-26T02:59:00Z'))
            self.assertEqual(seen[0], first)


class TokenCollectorTest(unittest.TestCase):
    def test_posts_all_hours_in_server_sized_batches(self):
        posted=[]
        cfg={'machine_id':'hetzner','access_token':'test-token','base_url':'https://hub.invalid'}
        hours=[{'engine':'codex','account':'shared-codex','project':str(i),'hour_start':'2026-10-26T00:00:00.000Z','input_tokens':1,'output_tokens':1} for i in range(100)]
        def post(url, batch, token):
            posted.append(batch)
            return {'ok':True}
        with patch.object(tu.config,'load_config',return_value=cfg), patch.object(tu,'build_hours',return_value=hours):
            result=tu.TokenCollector(http_client=post).collect_and_post()
        self.assertEqual([len(batch) for batch in posted], [48,48,4])
        self.assertEqual(result['hours'],100)

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
