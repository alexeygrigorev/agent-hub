import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host.native_progress import NativeTranscriptProgress
from agent_hub_host.session_progress import SessionProgress, orphan_reap_allowed
from agent_hub_host.resource_management import classify_reason


class IdleGoalProgressTests(unittest.TestCase):
    def test_top_level_goal_event_and_malformed_rows_do_not_hide_latest_status(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bound.jsonl'
            row = dict(id='own-native', created_at_ms=1000, engine='codex')
            sampler = NativeTranscriptProgress(roots=lambda _: [])
            sampler.paths[('own-native', 1000)] = path
            events = [[], None, dict(type='thread_goal_updated', goal=dict(status='stalled'))]
            path.write_text('\n'.join(map(json.dumps, events)) + '\n')
            self.assertEqual(sampler.goal_status(row), 'stalled')

    def test_rollout_goal_updates_and_tool_calls_use_latest_status(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bound.jsonl'
            row = dict(id='own-native', created_at_ms=1000, engine='codex')
            sampler = NativeTranscriptProgress(roots=lambda _: [])
            sampler.paths[('own-native', 1000)] = path
            events = [dict(type='event_msg', payload=dict(type='thread_goal_updated', goal=dict(status='paused')))]
            path.write_text('\n'.join(map(json.dumps, events)) + '\n')
            self.assertEqual(sampler.goal_status(row), 'paused')
            events.append(dict(type='response_item', payload=dict(type='function_call', name='tools.update_goal', arguments=json.dumps(dict(status='blocked')))))
            path.write_text('\n'.join(map(json.dumps, events)) + '\n')
            self.assertEqual(sampler.goal_status(row), 'blocked')
            events.append(dict(type='event_msg', payload=dict(type='thread_goal_updated', goal=dict(status='active'))))
            path.write_text('\n'.join(map(json.dumps, events)) + '\n')
            self.assertEqual(sampler.goal_status(row), 'active')
            # Transcript prose mentioning a tool is not an executed goal update.
            path.write_text(path.read_text() + json.dumps(dict(type='response_item', payload=dict(type='message', text='update_goal status blocked'))) + '\n')
            self.assertEqual(sampler.goal_status(row), 'active')

    def codex_home_rollout(self, temp, thread, events):
        import sqlite3
        home = Path(temp) / '.zcodex'
        path = home / 'sessions/2026/10/29' / f'rollout-2026-10-29T03-18-22-{thread}.jsonl'
        path.parent.mkdir(parents=True)
        meta = dict(type='session_meta', payload=dict(id=thread, cwd='/w', timestamp='2026-10-29T02:18:22.745Z'))
        path.write_text('\n'.join(map(json.dumps, [meta] + events)) + '\n')
        with sqlite3.connect(home / 'goals_1.sqlite') as db:
            db.execute('CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT, status TEXT, '
                       'token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)')
        return home, path

    def set_stored_goal(self, home, thread, status):
        import sqlite3
        with sqlite3.connect(home / 'goals_1.sqlite') as db:
            db.execute("INSERT OR REPLACE INTO thread_goals VALUES (?, 'g', 'objective', ?, NULL, 0, 0, 1, 2)", (thread, status))

    def test_codex_goal_store_is_authoritative_for_the_bound_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            # The creating event scrolled out of a long rollout's 256 KiB tail.
            home, path = self.codex_home_rollout(temp, 'own-thread', [
                dict(type='event_msg', payload=dict(type='thread_goal_updated', goal=dict(status='active'))),
                dict(type='response_item', payload=dict(type='message', role='assistant', content=[dict(type='output_text', text='x' * 300000)]))])
            row = dict(id='own-native', created_at_ms=1000, engine='zcodex')
            sampler = NativeTranscriptProgress(roots=lambda _: [])
            sampler.paths[('own-native', 1000)] = path
            self.assertIsNone(sampler.goal_status(row))
            self.set_stored_goal(home, 'other-thread', 'blocked')
            self.assertIsNone(sampler.goal_status(row))
            for status in ('active', 'blocked', 'usage_limited', 'budget_limited', 'complete'):
                self.set_stored_goal(home, 'own-thread', status)
                self.assertEqual(sampler.goal_status(row), status)
            # Store wins over a stale rollout event too.
            path.write_text(path.read_text() + json.dumps(dict(type='event_msg', payload=dict(type='thread_goal_updated', goal=dict(status='active')))) + '\n')
            self.assertEqual(sampler.goal_status(row), 'complete')

    def test_goal_launch_sent_as_plain_text_reports_unregistered(self):
        with tempfile.TemporaryDirectory() as temp:
            _, path = self.codex_home_rollout(temp, 'own-thread', [
                dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='# AGENTS.md instructions')])),
                dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='/goal Hub session rules:\nDo the work')]))])
            row = dict(id='own-native', created_at_ms=1000, engine='zcodex')
            sampler = NativeTranscriptProgress(roots=lambda _: [])
            sampler.paths[('own-native', 1000)] = path
            self.assertEqual(sampler.goal_status(row), 'unregistered')
            # An unreadable store proves nothing: never claim unregistered.
            store = Path(temp) / '.zcodex/goals_1.sqlite'
            store.rename(store.with_suffix('.moved'))
            self.assertIsNone(sampler.goal_status(row))
            store.write_bytes(b'not a sqlite database' * 10)
            self.assertIsNone(sampler.goal_status(row))
            store.unlink()
            store.with_suffix('.moved').rename(store)
            self.assertEqual(sampler.goal_status(row), 'unregistered')
            # A later executed goal update means Codex does track a goal.
            path.write_text(path.read_text() + json.dumps(dict(type='event_msg', payload=dict(type='thread_goal_updated', goal=dict(status='paused')))) + '\n')
            self.assertEqual(sampler.goal_status(row), 'paused')

    def test_plain_session_without_goal_text_stays_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            _, path = self.codex_home_rollout(temp, 'own-thread', [
                dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text='please mention /goal in docs')]))])
            row = dict(id='own-native', created_at_ms=1000, engine='codex')
            sampler = NativeTranscriptProgress(roots=lambda _: [])
            sampler.paths[('own-native', 1000)] = path
            self.assertIsNone(sampler.goal_status(row))

    @patch('agent_hub_host.session_progress.process', return_value={'pid': 1})
    def test_goal_stalled_screen_is_metadata_not_an_enter_or_stop(self, _):
        row = dict(id='own-native', created_at_ms=1000, engine='codex', tag='hub-own', workspace='/own', workload_pid=1, command=['codex'])
        stopped = []
        sampler = SessionProgress(now_ms=lambda: 100000, output=lambda _: None,
                                  screen=lambda _: 'Ask Codex to do anything\nGoal stalled (/goal resume)',
                                  stop=lambda r: stopped.append(r))
        result = sampler.sample([row])['own-native']
        self.assertEqual(result['goal_status'], 'stalled')
        self.assertIn('sampled_at',result)
        self.assertNotIn('terminal_error', result)
        self.assertEqual(stopped, [])

    def test_sampling_timestamp_requires_real_output_observation(self):
        row=dict(id='own-native',created_at_ms=1000,engine='codex',command=['codex'])
        known=SessionProgress(now_ms=lambda:100000,output=lambda _:5000).sample([row])['own-native']
        unknown=SessionProgress(now_ms=lambda:100000,output=lambda _:None).sample([row])['own-native']
        self.assertEqual(known['sampled_at'],'1970-01-01T00:01:40Z')
        self.assertNotIn('sampled_at',unknown)

    def test_orphan_permission_requires_exact_binding_and_hub_permission(self):
        row = dict(id='native', created_at_ms=1000, tag='own', workspace='/own')
        entry = dict(native_id='native', native_created_at_ms=1000, tag='own', workspace='/own', managed=True, idle_reap_allowed=True)
        request = lambda *_: dict(sessions=[entry])
        self.assertTrue(orphan_reap_allowed(request, row))
        entry['idle_reap_allowed'] = False
        self.assertFalse(orphan_reap_allowed(request, row))
        entry['idle_reap_allowed'] = True
        entry['native_created_at_ms'] = 2000
        self.assertFalse(orphan_reap_allowed(request, row))
        self.assertFalse(orphan_reap_allowed(lambda *_: {}, row))

    def test_transcript_history_mention_is_not_stalled_screen_marker(self):
        row = dict(id='own-native', created_at_ms=1000, engine='codex', workspace='/own', command=['codex'])
        sampler = SessionProgress(now_ms=lambda:100000, output=lambda _:None,
                                  screen=lambda _:'Earlier Goal stalled (/goal resume) was fixed.\n'+'history\n'*10+'Ask Codex to do anything')
        self.assertNotIn('goal_status',sampler.sample([row])['own-native'])

    def test_idle_reaper_fails_closed_for_open_or_unknown_linked_work(self):
        entry = dict(state='running', intake_id='open', last_activity_at='2026-10-27T00:00:00Z')
        now = 1793088000000
        self.assertIsNone(classify_reason(entry, 'running', False, 60000, now))
        entry['idle_reap_allowed'] = False
        self.assertIsNone(classify_reason(entry, 'running', False, 60000, now))
        entry['idle_reap_allowed'] = True
        self.assertEqual(classify_reason(entry, 'running', False, 60000, now), 'idle')


if __name__ == '__main__':
    unittest.main()
