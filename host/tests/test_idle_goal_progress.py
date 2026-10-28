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
