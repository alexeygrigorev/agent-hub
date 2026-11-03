"""Grok Bot replica watcher (intake 290b935a): blob selection, both-sides extraction,
catch-up, gap reporting, posting and the local A13 JSONL ledger."""
import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_hub_host import replica_watcher as rw

DEV = 'e96e39a2-c10d-48ea-a25b-e2c4240cf3ef'
ORIN = '1b5e58df-0000-4000-8000-000000000001'
FIXTURES = Path(__file__).parent / 'fixtures'
A13_FIELDS = ['agent_id', 'agent_name', 'msg_id', 't_id', 'role', 'kind', 'ts_utc', 'ts_berlin',
              'text', 'reply_to', 'attachments', 'source']


def blob_name(key):
    return base64.b32encode(key.encode('utf-8')).decode('ascii').rstrip('=').lower() + '.blob'


def replica_key(agent):
    return 'sand.client.slice.account.grok|alexe' + rw.REPLICA_MARKER + agent


def entry(msg_id, text='hello', ts=1793271700000, kind='message', **extra):
    return dict({'id': msg_id, 'kind': kind, 'text': text, 'ts': ts}, **extra)


class FakeHub:
    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    def __call__(self, method, path, payload=None):
        if self.fail:
            raise OSError('offline')
        self.posts.append((method, path, json.loads(json.dumps(payload))))
        count = sum(len(a['messages']) for a in payload['agents'])
        return {'ok': True, 'accepted': count, 'duplicates': 0, 'conflicts': [], 'rejected': [], 'gaps': []}

    def sent(self, agent_id, index=-1):
        return [m['msg_id'] for a in self.posts[index][2]['agents'] if a['agent_id'] == agent_id
                for m in a['messages']]


class ReplicaWatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / 'sand-client-persistence'
        self.root.mkdir()
        self.journal = base / 'journal'
        self.journal.mkdir()
        self.now = [1793271800.0]
        self.mono = [1000.0]
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, seconds, sleep=False):
        self.now[0] += seconds
        if not sleep:
            self.mono[0] += seconds

    def write(self, agent, entries):
        data = {'schemaVersion': 1, 'value': {'entries': entries}}
        (self.root / blob_name(replica_key(agent))).write_text(json.dumps(data), encoding='utf-8')

    def watcher(self, hub):
        return rw.ReplicaWatcher(hub, 'machine-1', self.journal, root=self.root,
                                 clock=lambda: self.now[0], monotonic=lambda: self.mono[0],
                                 log=lambda line, **_: self.logs.append(line))

    def test_decodes_only_replica_blobs_and_never_opens_others(self):
        secret = self.root / blob_name('sand.client.slice.account.grok|alexe.secrets.push')
        secret.write_text('{"token": "must-not-be-read"}', encoding='utf-8')
        (self.root / 'Local Storage').mkdir()
        (self.root / 'notbase32!.blob').write_text('x', encoding='utf-8')
        self.write(DEV, [entry('t1u')])
        opened = []
        original = rw.read_entries
        rw.read_entries = lambda path: opened.append(Path(path).name) or original(path)
        try:
            agents, _, _ = self.watcher(FakeHub()).snapshot()
        finally:
            rw.read_entries = original
        self.assertEqual([a['agent_id'] for a in agents], [DEV])
        self.assertEqual(opened, [blob_name(replica_key(DEV))])
        self.assertIsNone(rw.replica_agent(secret.name))

    def test_extracts_both_sides_with_reply_and_attachment_refs(self):
        entries = [entry('t10u', 'question'),
                   entry('t10s1', 'answer one', kind='send-message'),
                   entry('t10s2', 'answer two', kind='send-message', replyTo='t10u'),
                   {'id': 't11e', 'kind': 'event', 'ts': 1793271700000},
                   entry('t12u', None, kind='user-attachment',
                         attachments=[{'name': 'shot.png', 'mimeType': 'image/png', 'size': 1234,
                                       'id': 'att-1', 'data': 'BASE64-CONTENT'}]),
                   entry('t13u', 'Option B', kind='widget-answer', createdAt='2026-10-29T10:00:00Z')]
        window, messages = rw.extract(entries)
        diag = window.pop('diag')
        self.assertEqual(window, {'entry_count': 6, 'first_msg_id': 't10u', 'last_msg_id': 't13u',
                                  'first_seq': 10, 'last_seq': 13})
        self.assertEqual([(m['msg_id'], m['role'], m['reply_to']) for m in messages],
                         [('t10u', 'user', None), ('t10s1', 'assistant', 't10u'), ('t10s2', 'assistant', 't10u'),
                          ('t12u', 'user', None), ('t13u', 'user', None)])
        attachment = [m for m in messages if m['msg_id'] == 't12u'][0]
        self.assertEqual(attachment['attachments'],
                         [{'name': 'shot.png', 'mime': 'image/png', 'size': 1234, 'ref': 'att-1'}])
        self.assertIsNone(attachment['text'])
        self.assertNotIn('BASE64-CONTENT', json.dumps(messages))
        self.assertEqual(messages[-1]['kind'], 'widget-answer')
        self.assertEqual(diag['user_entries'], 3)
        self.assertEqual(diag['assistant_entries'], 2)
        self.assertEqual(diag['kinds']['event'], 1)

    def test_diag_counts_unparsed_entries_without_values(self):
        window, messages = rw.extract([{'id': 't3u', 'kind': 'message', 'blob': {'opaque': 'SECRET'}}])
        self.assertEqual(messages, [])
        self.assertEqual(window['diag']['without_text'], 1)
        self.assertEqual(window['diag']['without_ts'], 1)
        self.assertNotIn('SECRET', json.dumps(window))

    def test_text_is_verbatim_from_nested_parts(self):
        e = {'id': 't5u', 'content': [{'type': 'text', 'text': 'line one'}, {'type': 'text', 'text': 'line two'}],
             'createdAt': 1793271700}
        _, messages = rw.extract([e])
        self.assertEqual(messages[0]['text'], 'line one\nline two')
        self.assertEqual(messages[0]['ts'], 1793271700000)

    def test_posts_all_agents_then_only_new_messages(self):
        self.write(DEV, [entry('t1u'), entry('t1s1', kind='send-message'), entry('t2u')])
        self.write(ORIN, [entry('t7u', 'orin ask')])
        hub = FakeHub()
        watcher = self.watcher(hub)
        first = watcher.step()
        self.assertEqual(first['accepted'], 4)
        self.assertEqual(first['reason'], 'startup')
        body = hub.posts[0][2]
        self.assertEqual((hub.posts[0][0], hub.posts[0][1]), ('POST', '/api/asks/ingest'))
        self.assertEqual((body['source'], body['machine_id'], body['reason']), ('tabo-replica', 'machine-1', 'startup'))
        self.assertEqual({a['agent_id']: a['agent_name'] for a in body['agents']}, {DEV: 'Developer', ORIN: 'Orin'})
        self.tick(30)
        self.assertEqual(watcher.step()['skipped'], 'unchanged')
        self.write(DEV, [entry('t1u'), entry('t1s1', kind='send-message'), entry('t2u'),
                         entry('t2s1', kind='send-message'), entry('t3u', 'new')])
        self.tick(30)
        watcher.step()
        self.assertEqual(hub.sent(DEV), ['t2s1', 't3u'])
        self.assertEqual(hub.posts[-1][2]['reason'], 'poll')

    def test_heartbeat_post_when_idle(self):
        self.write(DEV, [entry('t1u')])
        hub = FakeHub()
        watcher = self.watcher(hub)
        watcher.step()
        self.tick(rw.HEARTBEAT_S + 1)
        watcher.step()
        self.assertEqual(len(hub.posts), 2)
        self.assertEqual(hub.posts[1][2]['reason'], 'heartbeat')
        self.assertEqual(hub.posts[1][2]['agents'][0]['messages'], [])

    def test_offline_catchup_after_sleep_ingests_everything_and_reports_overflow(self):
        self.write(DEV, [entry(f't{i}u') for i in range(1, 6)])
        hub = FakeHub()
        watcher = self.watcher(hub)
        watcher.step()
        self.tick(30)
        watcher.step()
        # Box sleeps 3 h; phone messages t6..t8 still in the window: no gap.
        self.write(DEV, [entry(f't{i}u') for i in range(3, 9)])
        self.tick(3 * 3600, sleep=True)
        result = watcher.step()
        self.assertEqual(result['reason'], 'resume')
        self.assertEqual(hub.sent(DEV), ['t6u', 't7u', 't8u'])
        self.assertIsNone(hub.posts[-1][2]['agents'][0]['gap'])
        self.assertEqual(watcher.interval(), rw.CATCHUP_POLL_S)
        # Longer outage: the window rotated past everything acknowledged.
        self.write(DEV, [entry(f't{i}u') for i in range(20, 23)])
        self.tick(24 * 3600, sleep=True)
        watcher.step()
        agent = hub.posts[-1][2]['agents'][0]
        self.assertEqual(agent['gap'], {'after_msg_id': 't8u', 'after_seq': 8, 'first_seq_in_window': 20})
        self.assertEqual([m['msg_id'] for m in agent['messages']], ['t20u', 't21u', 't22u'])
        self.tick(rw.CATCHUP_S + 1)
        self.assertEqual(watcher.interval(), rw.POLL_S)

    def test_watcher_stopped_then_restarted_ingests_all_without_gaps(self):
        self.write(DEV, [entry('t1u')])
        hub = FakeHub()
        self.watcher(hub).step()
        # Watcher stopped; messages arrive meanwhile; a new process starts.
        self.write(DEV, [entry('t1u'), entry('t2u'), entry('t2s1', kind='send-message'), entry('t3u')])
        restarted = self.watcher(hub)
        result = restarted.step()
        self.assertEqual(result['reason'], 'startup')
        self.assertEqual(hub.sent(DEV), ['t2u', 't2s1', 't3u'])
        self.assertIsNone(hub.posts[-1][2]['agents'][0]['gap'])

    def test_failure_keeps_messages_for_the_next_attempt(self):
        self.write(DEV, [entry('t1u')])
        result = self.watcher(FakeHub(fail=True)).step()
        self.assertEqual(result['event'], 'replica_watcher_error')
        hub = FakeHub()
        self.watcher(hub).step()
        self.assertEqual(hub.sent(DEV), ['t1u'])

    def test_local_jsonl_is_append_only_deduplicated_a13(self):
        self.write(DEV, [entry('t1u', 'Привет', ts=1793271700000), entry('t1s1', 'hi', kind='send-message')])
        hub = FakeHub(fail=True)
        watcher = self.watcher(hub)
        watcher.step()   # Hub down: the local copy is still written
        hub.fail = False
        self.tick(rw.MAX_BACKOFF_S + 1)
        watcher.step()
        self.watcher(FakeHub()).step()   # restart re-reads keys, no duplicates
        rows = [json.loads(line) for line in (self.journal / rw.LEDGER_FILENAME).read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['msg_id'] for r in rows], ['t1u', 't1s1'])
        user = rows[0]
        self.assertEqual(list(user), A13_FIELDS)
        self.assertEqual(user['text'], 'Привет')
        self.assertEqual(user['ts_utc'], '2026-10-29T11:01:40Z')
        self.assertEqual(user['ts_berlin'], '2026-10-29T12:01:40+01:00')
        self.assertEqual(rows[1]['reply_to'], 't1u')
        self.assertEqual(user['source'], 'tabo-replica')

    def test_berlin_fallback_rule_matches_dst(self):
        from datetime import datetime, timezone, timedelta
        summer = datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc)
        winter = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(rw._berlin_offset(summer), timedelta(hours=2))
        self.assertEqual(rw._berlin_offset(winter), timedelta(hours=1))
        self.assertEqual(rw._berlin_offset(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)), timedelta(hours=2))

    def test_sample_fixture_matches_schema(self):
        rows = [json.loads(line) for line in (FIXTURES / 'replica-ledger-sample.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(list(row), A13_FIELDS)
            self.assertIn(row['role'], ('user', 'assistant', 'system'))
        self.assertEqual(len({(r['agent_id'], r['msg_id']) for r in rows}), len(rows))

    def test_logs_never_contain_text(self):
        self.write(DEV, [entry('t1u', 'SECRET-ASK-TEXT')])
        self.watcher(FakeHub()).step()
        self.assertTrue(self.logs)
        self.assertNotIn('SECRET-ASK-TEXT', '\n'.join(self.logs))

    def test_build_posts_respects_limits_and_order(self):
        messages = [{'msg_id': f't{i}u', 't_id': f't{i}', 'seq': i, 'role': 'user', 'kind': 'message',
                     'ts': None, 'text': 'x' * 2000, 'reply_to': None, 'attachments': []}
                    for i in range(1, 451)]
        posts = rw.build_posts('m', [{'agent_id': DEV, 'agent_name': 'Developer', 'gap': None,
                                      'window': {'entry_count': 450}, 'messages': messages}], 1)
        sent = [m['msg_id'] for p in posts for a in p['agents'] for m in a['messages']]
        self.assertEqual(sent, [m['msg_id'] for m in messages])
        for post in posts:
            self.assertLessEqual(len(json.dumps(post).encode('utf-8')), rw.MAX_BODY_BYTES)
            for agent in post['agents']:
                self.assertLessEqual(len(agent['messages']), rw.MAX_MESSAGES_PER_AGENT)
        many = [{'agent_id': f'a{i}', 'agent_name': None, 'gap': None, 'window': {'entry_count': 0}, 'messages': []}
                for i in range(40)]
        posts = rw.build_posts('m', many, 1)
        self.assertTrue(all(len(p['agents']) <= rw.MAX_AGENTS_PER_POST for p in posts))
        self.assertEqual(sum(len(p['agents']) for p in posts), 40)

    def test_disabled_without_directory_or_by_config(self):
        missing = rw.ReplicaWatcher(FakeHub(), 'm', self.journal, root=self.root / 'absent')
        self.assertFalse(missing.enabled())
        (self.journal / rw.CONFIG_FILENAME).write_text('{"enabled": false}', encoding='utf-8')
        self.assertFalse(self.watcher(FakeHub()).enabled())
        self.watcher(FakeHub()).loop(threading.Event())  # returns immediately when disabled

    def test_corrupt_blob_is_skipped(self):
        (self.root / blob_name(replica_key(DEV))).write_text('{not json', encoding='utf-8')
        self.write(ORIN, [entry('t1u')])
        result = self.watcher(FakeHub()).step()
        self.assertEqual(result['skipped_blobs'], 1)
        self.assertEqual(result['accepted'], 1)


if __name__ == '__main__':
    unittest.main()
