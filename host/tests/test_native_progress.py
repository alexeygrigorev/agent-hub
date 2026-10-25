import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest

from agent_hub_host.native_progress import NativeTranscriptProgress


class NativeProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start = int(dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc).timestamp() * 1000)
        self.row = dict(id='native', engine='codex', created_at_ms=self.start, cwd='/repo', workspace='/repo')
        self.sampler = NativeTranscriptProgress(roots=lambda _: [self.root], proc_root=self.root / 'proc')

    def rollout(self, name='rollout-one.jsonl', *, cwd='/repo', offset=0, day='2026/10/25'):
        path = self.root / day / name
        path.parent.mkdir(parents=True, exist_ok=True)
        at = dt.datetime.fromtimestamp((self.start + offset) / 1000, dt.timezone.utc).isoformat()
        path.write_text(json.dumps(dict(type='session_meta', payload=dict(timestamp=at, cwd=cwd))) + '\n')
        os.utime(path, (self.start / 1000 + 60, self.start / 1000 + 60))
        return path

    def test_frozen_rollout_survives_terminal_chatter_and_advances_on_native_write(self):
        path = self.rollout()
        first = self.sampler(self.row)
        self.assertEqual(first, self.start + 60000)
        self.row['last_activity_ms'] = self.start + 4 * 3600000
        self.assertEqual(self.sampler(self.row), first)
        path.write_text(path.read_text() + '{"type":"event_msg"}\n')
        self.assertGreater(self.sampler(self.row), first)

    def test_shared_cwd_different_launch_and_ambiguous_candidates_are_not_progress(self):
        self.rollout(offset=-3600000)
        self.assertIsNone(self.sampler(self.row))
        self.rollout('rollout-two.jsonl')
        self.rollout('rollout-three.jsonl', offset=1000)
        self.assertIsNone(self.sampler(self.row))

    def test_midnight_and_discovery_of_already_hung_session(self):
        self.rollout(offset=-1000, day='2026/10/24')
        self.assertEqual(self.sampler(self.row), self.start + 60000)

    def test_foreign_cwd_and_malformed_metadata_are_not_progress(self):
        self.rollout(cwd='/other')
        self.assertIsNone(self.sampler(self.row))
        self.rollout().write_text('invalid\n')
        self.assertIsNone(self.sampler(self.row))

    def test_bound_transcript_is_used_without_mutating_native_sidecar(self):
        path = self.rollout(cwd='/other', offset=-3600000)
        parent = self.root / 'sessions/native'
        parent.mkdir(parents=True)
        bound = parent / 'transcript.json'
        bound.write_text(json.dumps({'path': str(path)}))
        before = bound.stat().st_mtime_ns
        self.row['history_path'] = str(parent / 'history.bin')
        self.assertEqual(self.sampler(self.row), self.start + 60000)
        self.assertEqual(bound.stat().st_mtime_ns, before)

    def test_missing_rollout_does_not_fabricate_activity(self):
        path = self.rollout()
        self.assertIsNotNone(self.sampler(self.row))
        path.unlink()
        self.assertIsNone(self.sampler(self.row))

    def test_opencode_uses_exact_session_message_events_not_database_churn(self):
        db = self.root / 'opencode/opencode.db'
        db.parent.mkdir()
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE session_v2 (id TEXT, directory TEXT, parent_id TEXT, time_created INTEGER)')
            conn.execute('CREATE TABLE session_message (session_id TEXT, time_updated INTEGER)')
            conn.execute('INSERT INTO session_v2 VALUES (?, ?, NULL, ?)', ('ours', '/repo', self.start))
            conn.execute('INSERT INTO session_v2 VALUES (?, ?, NULL, ?)', ('other', '/other', self.start))
            conn.execute('INSERT INTO session_message VALUES (?, ?)', ('ours', self.start + 60000))
            conn.execute('INSERT INTO session_message VALUES (?, ?)', ('other', self.start + 600000))
        sampler = NativeTranscriptProgress(opencode_data=self.root)
        row = dict(self.row, engine='opencode')
        self.assertEqual(sampler(row), self.start + 60000)
        with sqlite3.connect(db) as conn:
            conn.execute('UPDATE session_message SET time_updated = ? WHERE session_id = ?', (self.start + 3600000, 'other'))
        self.assertEqual(sampler(row), self.start + 60000)
        with sqlite3.connect(db) as conn:
            conn.execute('UPDATE session_message SET time_updated = ? WHERE session_id = ?', (self.start + 120000, 'ours'))
        self.assertEqual(sampler(row), self.start + 120000)
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO session_v2 VALUES (?, ?, NULL, ?)', ('ambiguous', '/repo', self.start))
        self.assertIsNone(sampler(row))


if __name__ == '__main__':
    unittest.main()
