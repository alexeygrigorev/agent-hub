"""Claude native Stop-hook goal receipts must identify the launched conversation."""
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock
import subprocess
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import claude_goal as cg
from agent_hub_host import native_launch as nl


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ClaudeGoalStoreTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[3] / '.local/tmp'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, prefix='claude-goal-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = str(self.root / 'workspace')
        self.command = cg.conversation_argv(['claude', '--model', 'opus'], 'task', 'operation:0', 1)
        self.thread = self.command[-1]
        self.row = {'workspace': self.workspace, 'created_at_ms': int(time.time() * 1000), 'command': self.command}
        self.path = self.root / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', self.workspace) / (self.thread + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.event = {'type': 'attachment', 'isSidechain': False, 'sessionId': self.thread,
                      'cwd': self.workspace, 'timestamp': datetime.now(timezone.utc).isoformat(),
                      'attachment': {'type': 'goal_status', 'met': False, 'sentinel': True, 'condition': 'finish objective'}}

    def write(self, *events):
        self.path.write_text(''.join(json.dumps(event) + '\n' for event in events))

    def read(self):
        return cg.goal_store(self.row, self.root, self.thread, digest)

    def test_status_cannot_replace_the_launch_pinned_conversation(self):
        start = {**self.row, 'id': 'd6cf9872-8597-48a5-be64-990cdef4db31',
                 'tag': 'task-launch', 'engine': 'claude', 'phase': 'running'}
        other = cg.conversation_argv(['claude', '--model', 'opus'], 'other-task', 'other-operation', 1)
        other_thread = other[-1]
        self.path.with_name(other_thread + '.jsonl').write_text(json.dumps({
            **self.event, 'sessionId': other_thread}) + '\n')
        status = {**start, 'command': other}
        adapter = nl.NativeLauncher({})
        with mock.patch.object(adapter, 'status', return_value=status), \
             mock.patch.object(adapter, 'native') as send:
            with self.assertRaisesRegex(RuntimeError, '^native_goal_identity_unverified$'):
                adapter.register_goal(start, {'workspace': self.workspace}, 'finish objective',
                                      time.time() + 10, claude_config_dir=self.root)
            send.assert_not_called()
        self.assertIsNone(cg.goal_store(status, self.root, self.thread, digest))

    def test_attempt_identity_is_stable_but_never_shared_with_another_attempt(self):
        self.assertEqual(self.command, cg.conversation_argv(self.command[:-2], 'task', 'operation:0', 1))
        for task, operation, epoch in [('other', 'operation:0', 1), ('task', 'operation:1', 1), ('task', 'operation:0', 2)]:
            self.assertNotEqual(self.thread, cg.conversation_argv([], task, operation, epoch)[-1])

    def test_native_sentinel_proves_exact_goal_registration(self):
        self.write(self.event)
        self.assertEqual(self.read(), {'status': 'active', 'thread_id': self.thread, 'objective_hash': digest('finish objective')})

    def test_user_text_and_model_claims_do_not_prove_registration(self):
        for kind in ['user', 'assistant']:
            self.write({**self.event, 'type': kind, 'message': {'content': '/goal finish objective'}})
            self.assertIsNone(self.read())

    def test_other_conversations_workspaces_sidechains_and_stale_events_do_not_count(self):
        for change in [{'sessionId': 'other'}, {'cwd': '/other'}, {'isSidechain': True},
                       {'timestamp': '2020-01-01T00:00:00Z'}, {'timestamp': '2100-01-01T00:00:00Z'},
                       {'timestamp': '2026-11-12T00:00:00'}]:
            with self.subTest(change=change):
                self.write({**self.event, **change})
                self.assertIsNone(self.read())

    def test_never_selects_a_nearby_transcript(self):
        self.write(self.event)
        self.path.rename(self.path.with_name('other.jsonl'))
        self.assertIsNone(self.read())

    def test_latest_native_state_supersedes_earlier_condition(self):
        newer = {**self.event, 'attachment': {**self.event['attachment'], 'condition': 'new objective'}}
        self.write(self.event, newer)
        self.assertEqual(self.read()['objective_hash'], digest('new objective'))

    def test_goal_evaluator_completion_failure_and_user_clear_are_distinct(self):
        for changes, status in [({'met': True, 'sentinel': False}, 'complete'),
                                ({'failed': True}, 'blocked'), ({'met': True}, 'paused')]:
            self.write(self.event, {**self.event, 'attachment': {**self.event['attachment'], **changes}})
            self.assertEqual(self.read()['status'], status)

    def test_malformed_partial_lines_and_unrelated_events_are_safe(self):
        self.write(self.event, None, {'type': 'attachment', 'attachment': None})
        with self.path.open('ab') as stream:
            stream.write(b'{"unfinished":')
        self.assertEqual(self.read()['status'], 'active')

    def test_missing_and_ambiguous_command_identity_fail_closed(self):
        self.write(self.event)
        for command in [[], ['claude', '--session-id'], ['claude', '--session-id', 'bad'], self.command + self.command[-2:]]:
            self.row['command'] = command
            self.assertIsNone(self.read())

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'requires no-follow open')
    def test_symlink_and_oversized_transcripts_are_rejected(self):
        self.write(self.event)
        other = self.path.with_suffix('.saved')
        self.path.rename(other)
        self.path.symlink_to(other)
        self.assertIsNone(self.read())
        self.path.unlink()
        with self.path.open('wb') as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        self.assertIsNone(self.read())


class ClaudeGoalLaunchTest(unittest.TestCase):
    def test_opus_and_sonnet_install_native_goal_with_one_guarded_send(self):
        parent = Path(__file__).resolve().parents[3] / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        for model, private in itertools.product(['opus', 'sonnet'], [False, True]):
            with self.subTest(model=model, private=private), tempfile.TemporaryDirectory(dir=parent) as temporary:
                root = Path(temporary)
                workspace = str(root / 'workspace')
                Path(workspace).mkdir()
                config = root / 'claude-config'
                row, sends = {}, []
                native_id = 'd6cf9872-8597-48a5-be64-990cdef4db31'
                spec = {'workspace': workspace, 'engine': 'claude', 'model': model,
                        'prompt': 'Complete the exact objective', 'interactive': True,
                        'with_goal': True, 'effort': 'medium', 'timeout_seconds': 600,
                        'session_id': 'session', 'operation_id': 'operation:0', 'epoch': 1}
                if private:
                    spec.update(provider_home=str(root / 'provider'), client=mock.Mock())

                def runner(argv, **options):
                    if argv[1] == 'start':
                        prefix = argv[:argv.index('--')]
                        child = argv[argv.index('--') + 1:]
                        self.assertEqual(prefix[prefix.index('--engine') + 1], 'claude')
                        self.assertNotIn(spec['prompt'], child)
                        self.assertNotIn('-p', child)
                        self.assertEqual(child[child.index('--model') + 1], model)
                        self.assertIn('CLAUDE_CONFIG_DIR=' + str(config), prefix)
                        row.update(id=native_id, tag='task-launch', workspace=workspace,
                                   engine='claude', command=child, created_at_ms=int(time.time() * 1000), phase='running')
                        payload = row
                    elif argv[1] == 'status':
                        payload = row
                    elif argv[1] == 'send':
                        sends.append(argv)
                        self.assertEqual(argv[2], native_id)
                        self.assertTrue(argv[3].startswith('/goal '))
                        thread = row['command'][row['command'].index('--session-id') + 1]
                        transcript = config / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', workspace) / (thread + '.jsonl')
                        transcript.parent.mkdir(parents=True)
                        transcript.write_text(json.dumps({'type': 'attachment', 'isSidechain': False,
                            'cwd': workspace, 'sessionId': thread, 'timestamp': datetime.now(timezone.utc).isoformat(),
                            'attachment': {'type': 'goal_status', 'met': False, 'sentinel': True, 'condition': argv[3][6:]}}) + '\n')
                        payload = {'id': native_id, 'delivery': {'engine': 'claude', 'ready': True, 'landed': True,
                                   'submitted': True, 'status': 'submitted', 'sends': 1, 'enter_presses': 1}}
                    else:
                        self.fail('unexpected native command')
                    return subprocess.CompletedProcess(argv, 0, json.dumps(payload), '')

                adapter = nl.NativeLauncher({'private_provider_homes': private,
                    'routes': {'claude': {'provider': 'claude'}}}, runner=runner)
                with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(config)}), \
                     mock.patch.object(nl, 'native_environment', return_value={'CLAUDE_CONFIG_DIR': str(config)}), \
                     mock.patch.object(nl, 'session_binding', return_value={'engine_binary_path': '/synthetic/claude',
                         'runtime_deadline': int(time.time() * 1000) + 500000}):
                    receipt = adapter.launch('launch', spec)
                self.assertEqual(len(sends), 1)
                self.assertEqual(receipt['goal_registration']['status'], 'active')
                self.assertEqual(receipt['goal_registration']['objective_hash'], nl.objective_hash(sends[0][3][6:]))
                self.assertTrue(receipt['interactive_verified'])
                self.assertEqual(receipt['model'], model)

    def test_available_claude_advertises_goal_without_losing_codex_or_zai(self):
        adapter = nl.NativeLauncher({})
        with mock.patch.object(nl.shutil, 'which', return_value='/synthetic/binary'):
            capabilities = adapter.capabilities()
        for provider in ['claude', 'codex', 'zai']:
            self.assertEqual(capabilities[provider], {'headless', 'interactive', 'goal', 'effort'})
