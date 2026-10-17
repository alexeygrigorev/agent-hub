"""Recovery tests use fake transports only here; HTTP/native evidence is separate."""
import copy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_conversation_bridge as base
from agent_hub_host import conversation_bridge as cb
from agent_hub_host.native_bridge import BridgeCapabilityGap, NativeServiceBinding


class TestGapRecovery(unittest.TestCase):
    client = base.TestConversationBridge.client
    setUp = base.TestConversationBridge.setUp

    def setUp(self):
        base.TestConversationBridge.setUp(self)
        self.last_seq = 0
        request = self.bridge.request
        def canonical(method, path, payload=None):
            result = request(method, path, payload)
            if method == 'GET' and '/messages?' in path:
                cursor = int(path.split('after_seq=')[1].split('&')[0])
                limit = int(path.split('limit=')[1])
                rows = sorted(result['messages'], key=lambda r: r['seq'])[:limit]
                retained = sorted(m['seq'] for m in self.hub.messages if m['conversation_id'] == 'c1')
                low, high = (retained[0], retained[-1]) if retained else (None, None)
                last = max(self.last_seq, high or 0)
                read = self.hub.reads.get(('project:worker', 'c1'), 0)
                result.update(messages=rows, last_seq=last, my_read_seq=read, read_seq=read,
                              oldest_seq=rows[0]['seq'] if rows else None,
                              newest_seq=rows[-1]['seq'] if rows else None,
                              oldest_retained_seq=low, newest_retained_seq=high,
                              next_after_seq=rows[-1]['seq'] if rows else max(cursor, last),
                              prev_before_seq=rows[0]['seq'] if rows else None,
                              has_more_before=bool(rows and rows[0]['seq'] > low),
                              has_more_after=bool(rows and rows[-1]['seq'] < high),
                              gap_detected=False, resume_after_seq=0)
            return result
        self.bridge.request = canonical
        self.bridge.cycle()  # old v1 journal, before any missing history
        self.hub.posts.clear()

    def resume(self, target=2):
        return self.bridge.resume('c1', target, acknowledge_gap=True)

    def state(self):
        return json.loads(self.bridge.path.read_text())

    def unchanged(self, operation, error=Exception):
        before = self.bridge.path.read_bytes()
        with self.assertRaises(error):
            operation()
        self.assertEqual(self.bridge.path.read_bytes(), before)

    def test_default_hold_then_prefix_resume_restart_delivers_once(self):
        self.hub.message(seq=3)
        self.unchanged(self.bridge.cycle, BridgeCapabilityGap)
        result = self.resume()
        self.assertEqual(result['status'], 'gap_recorded')
        self.assertIs(result['server_read_advanced'], False)
        self.assertFalse(self.hub.reads)
        self.assertEqual(self.native.sends, 0)
        self.assertFalse(any(p[1] == 'POST' for p in self.hub.posts))
        gap = self.state()['gaps'][0]
        self.assertEqual((gap['old_cursor'], gap['new_cursor'], gap['missing_from_seq'], gap['missing_through_seq']), (0, 2, 1, 2))
        self.assertEqual(gap['identity'][-2:], [self.native.binding.session_id, 'worker'])
        self.bridge.state = None  # process restart reloads the same immutable journal
        self.bridge.cycle()
        self.bridge.state = None
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertEqual(self.hub.reads[('project:worker', 'c1')], 3)
        self.assertEqual(set(self.state()['deliveries']), {'c1:3'})
        self.assertEqual([p[3]['seq'] for p in self.hub.posts if p[2].endswith('/read')], [3])

    def test_interior_gap_ignores_incorrect_api_gap_flags(self):
        self.hub.message(seq=1)
        self.bridge.cycle()
        self.hub.message(seq=3)
        self.unchanged(self.bridge.cycle, BridgeCapabilityGap)
        self.resume()
        self.assertEqual(self.state()['gaps'][0]['old_cursor'], 1)
        self.assertEqual(self.hub.reads[('project:worker', 'c1')], 1)
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 2)

    def test_all_pruned_tail_and_multiple_append_only_gaps(self):
        self.last_seq = 4
        self.resume(2)
        first = copy.deepcopy(self.state()['gaps'][0])
        self.resume(4)
        self.assertEqual(self.state()['gaps'][0], first)
        self.assertEqual(len(self.state()['gaps']), 2)
        self.assertFalse(self.hub.reads)

    def test_retained_range_and_page_boundaries_never_skip(self):
        for seq in range(2, 204):
            self.hub.message(seq=seq)
        for target in (2, 3, 100, 101, 102, 203):
            with self.subTest(target=target):
                self.unchanged(lambda: self.resume(target), BridgeCapabilityGap)
        self.resume(1)
        self.assertFalse(self.hub.reads)

    def test_invalid_target_types_future_backwards_and_equal_replays(self):
        self.last_seq = 3
        for value in (True, False, None, '2', 2.0, float('inf'), float('nan'), -1, cb.MAX_SEQ + 1, 0, 4):
            with self.subTest(value=value):
                self.unchanged(lambda: self.resume(value))
        self.resume(2)
        for value in (0, 1, 2):
            self.unchanged(lambda: self.resume(value), BridgeCapabilityGap)

    def test_explicit_ack_and_existing_journal_required(self):
        self.last_seq = 3
        for value in (False, None, 1, 'yes'):
            self.unchanged(lambda: self.bridge.resume('c1', 2, acknowledge_gap=value))
        self.bridge.path.unlink()
        with self.assertRaises(BridgeCapabilityGap):
            self.resume()
        self.assertFalse(self.bridge.path.exists())

    def test_malformed_pages_are_not_gap_authority(self):
        self.hub.message(seq=3)
        original = self.bridge.request
        mutations = [
            lambda p: p.update(messages=[]), lambda p: p.pop('messages'),
            lambda p: p.update(self_actor='foreign:worker'), lambda p: p.update(conversation_id='foreign'),
            lambda p: p.update(last_seq=True), lambda p: p.update(last_seq=1),
            lambda p: p.update(my_read_seq=True, read_seq=True),
            lambda p: p.update(read_seq=1), lambda p: p.update(has_more_after=True),
            lambda p: p.update(next_after_seq=99), lambda p: p.update(oldest_seq=2),
            lambda p: p.update(oldest_retained_seq=4), lambda p: p.pop('newest_retained_seq'),
            lambda p: p.update(messages=p['messages'] * 2),
            lambda p: p['messages'][0].update(seq=True),
            lambda p: p['messages'][0].update(conversation_id='wrong'),
            lambda p: p['messages'][0].update(body=123),
        ]
        for mutate in mutations:
            def changed(method, path, payload=None):
                result = original(method, path, payload)
                if '/messages?' in path and 'limit=100' in path:
                    mutate(result)
                return result
            self.bridge.request = changed
            with self.subTest(mutation=mutate):
                self.unchanged(self.resume)
        self.bridge.request = original

    def test_revoked_or_nonmember_auth_precedes_local_effects(self):
        self.hub.removed.add('project:worker')
        self.bridge.path.unlink()
        lock = Path(str(self.bridge.path) + '.lock')
        lock.unlink()
        with self.assertRaises(BridgeCapabilityGap):
            self.resume()
        self.assertFalse(lock.exists())
        self.assertFalse(self.bridge.path.exists())

    def test_foreign_credential_project_machine_and_native_rollover_hold(self):
        self.last_seq = 3
        saved = copy.deepcopy(self.hub.actors['project:worker'])
        for field, value in [('actor', 'foreign:worker'), ('project', 'foreign'), ('machine_id', 'foreign'),
                             ('native_binding', {'tag': 'worker', 'project': 'project', 'machine_id': 'machine-one', 'workspace': '/foreign'})]:
            self.hub.actors['project:worker'][field] = value
            self.unchanged(self.resume, BridgeCapabilityGap)
            self.hub.actors['project:worker'] = copy.deepcopy(saved)
        self.native.binding = NativeServiceBinding('foreign', 'worker', self.bridge.workspace)
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_membership_change_after_proof_and_watermark_rollback_hold(self):
        self.hub.message(seq=3)
        original = self.bridge.request
        calls = 0
        def revoke(method, path, payload=None):
            result = original(method, path, payload)
            if 'limit=100' in path:
                self.hub.removed.add('project:worker')
            return result
        self.bridge.request = revoke
        self.unchanged(self.resume, BridgeCapabilityGap)
        self.hub.removed.clear()
        def rollback(method, path, payload=None):
            nonlocal calls
            result = original(method, path, payload)
            if 'limit=100' in path:
                calls += 1
                result['last_seq'] = 4 if calls == 1 else 3
            return result
        self.bridge.request = rollback
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_native_binding_change_during_proof_holds(self):
        self.hub.message(seq=3)
        original = self.bridge.request
        def rollover(method, path, payload=None):
            result = original(method, path, payload)
            if 'limit=100' in path:
                self.native.binding = NativeServiceBinding('foreign', 'worker', self.bridge.workspace)
            return result
        self.bridge.request = rollover
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_uncertain_native_send_and_pending_read_never_erased(self):
        self.hub.message()
        self.native.error_without_effect = True
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.hub.messages.clear()
        self.last_seq = 3
        self.unchanged(self.resume, BridgeCapabilityGap)
        self.assertEqual(self.native.sends, 1)
        row = self.state()['deliveries']['c1:1']
        self.native.messages.append(self.native.envelope(row['body'], 'hub-conversation', data=row['data']))
        # Recovery still cannot pretend the pruned original was read.
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_unresolved_reply_intent_is_preserved(self):
        self.hub.message()
        self.bridge.cycle()
        self.native.reply()
        self.hub.send_error = True
        with self.assertRaises(OSError):
            self.bridge.cycle()
        self.hub.messages.clear()
        self.last_seq = 3
        self.unchanged(self.resume, BridgeCapabilityGap)
        self.assertFalse(next(iter(self.state()['replies'].values()))['done'])

    def test_old_v1_and_unknown_extension_evidence_preserved(self):
        state = self.state()
        self.assertNotIn('gaps', state)
        state['essential_extension'] = {'keep': ['evidence']}
        self.bridge.path.write_text(json.dumps(state))
        self.last_seq = 3
        self.resume()
        self.assertEqual(self.state()['essential_extension'], state['essential_extension'])

    def test_malformed_journal_cursor_version_records_and_gap_rollback_hold(self):
        baseline = self.state()
        variants = []
        for field, value in [('version', True), ('version', 2), ('cursors', {'c1': True}),
                             ('cursors', {'c1': -1}), ('deliveries', []), ('replies', {'x': {}}),
                             ('gaps', [{}]), ('gaps', 'bad')]:
            state = copy.deepcopy(baseline)
            state[field] = value
            variants.append(state)
        self.last_seq = 3
        for state in variants:
            self.bridge.path.write_text(json.dumps(state))
            self.unchanged(self.resume)
        self.bridge.path.write_text(json.dumps(baseline))
        self.resume()
        state = self.state()
        state['cursors']['c1'] = 0
        self.bridge.path.write_text(json.dumps(state))
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_full_gap_history_does_not_evict_audit(self):
        self.last_seq = 200
        with patch.object(cb, 'MAX_GAPS', 2):
            self.resume(1)
            self.resume(2)
            self.unchanged(lambda: self.resume(3), BridgeCapabilityGap)
        self.assertEqual(len(self.state()['gaps']), 2)

    def test_crash_before_atomic_replace_rolls_back_then_restart_recovers_once(self):
        self.hub.message(seq=3)
        with patch.object(cb.os, 'replace', side_effect=OSError('crash')):
            self.unchanged(self.resume, OSError)
        self.assertFalse(Path(str(self.bridge.path) + '.tmp').exists())
        self.bridge.state = None
        self.resume()
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)

    def test_crash_after_replace_leaves_atomic_record_and_refuses_replay(self):
        self.last_seq = 3
        original = cb.os.fsync
        calls = 0
        def crash(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('directory sync uncertain')
            return original(fd)
        with patch.object(cb.os, 'fsync', side_effect=crash), self.assertRaises(OSError):
            self.resume()
        self.assertEqual(self.state()['cursors']['c1'], 2)
        self.assertEqual(len(self.state()['gaps']), 1)
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_same_journal_lock_and_reload_prevent_stale_cursor_recovery(self):
        self.last_seq = 3
        with self.bridge.journal():
            self.unchanged(self.resume, BlockingIOError)
        self.resume()
        self.bridge.state['cursors']['c1'] = 0  # stale memory must not bypass disk
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_missing_empty_page_fields_duplicate_and_nonfinite_journal_hold(self):
        self.last_seq = 3
        original = self.bridge.request
        for key in ('oldest_retained_seq', 'newest_retained_seq', 'oldest_seq', 'newest_seq',
                    'prev_before_seq', 'has_more_before', 'next_after_seq'):
            def omit(method, path, payload=None):
                result = original(method, path, payload)
                if 'limit=100' in path:
                    result.pop(key)
                return result
            self.bridge.request = omit
            self.unchanged(self.resume, BridgeCapabilityGap)
        self.bridge.request = original
        baseline = self.bridge.path.read_text()
        for raw in (baseline[:-1] + ', "version": 1}', baseline[:-1] + ', "future": NaN}'):
            self.bridge.path.write_text(raw)
            self.unchanged(self.resume, BridgeCapabilityGap)
        self.bridge.path.write_text(baseline)

    def test_malformed_delivery_and_reply_records_hold_without_rewriting(self):
        self.hub.message()
        self.bridge.cycle()
        baseline = self.state()
        self.hub.messages.clear()
        self.last_seq = 3
        for field, value in [('attempted', 1), ('native_id', ''), ('body', 'changed'),
                             ('message', {'seq': 1}), ('data', {})]:
            state = copy.deepcopy(baseline)
            state['deliveries']['c1:1'][field] = value
            self.bridge.path.write_text(json.dumps(state))
            self.unchanged(self.resume)
        state = copy.deepcopy(baseline)
        del state['deliveries']['c1:1']['native_id']
        self.bridge.path.write_text(json.dumps(state))
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_size_and_response_budgets_refuse_without_truncating_state(self):
        self.last_seq = 3
        baseline = self.bridge.path.read_bytes()
        with patch.object(cb, 'MAX_STATE', len(baseline) - 1):
            self.unchanged(self.resume, BridgeCapabilityGap)
        # Fits the old state, but not the appended audit: no cursor-only write.
        with patch.object(cb, 'MAX_STATE', len(baseline) + 1):
            self.unchanged(self.resume, BridgeCapabilityGap)
        original = self.bridge.request
        def oversized(method, path, payload=None):
            result = original(method, path, payload)
            if 'limit=100' in path:
                result['oversized'] = 'x' * (2 * 1024 * 1024)
            return result
        self.bridge.request = oversized
        self.unchanged(self.resume, BridgeCapabilityGap)

    def test_unrelated_cli_and_help_do_not_import_unix_bridge(self):
        import subprocess
        import sys
        program = """
import sys
from unittest.mock import patch
sys.modules['fcntl'] = None  # Windows has no Unix flock module.
from agent_hub_host import cli, conversations
assert 'agent_hub_host.conversation_bridge' not in sys.modules
sys.argv = ['hub', 'conversations', 'participants']
with patch.object(conversations, 'execute', return_value={}):
    cli.main()
assert 'agent_hub_host.conversation_bridge' not in sys.modules
sys.argv = ['hub', 'bridge-resume', '--help']
try:
    cli.main()
except SystemExit as exc:
    assert exc.code == 0
assert 'agent_hub_host.conversation_bridge' not in sys.modules
"""
        result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                                     'PYTHONDONTWRITEBYTECODE': '1'}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('missing', result.stdout)

    def test_cli_opt_in_ack_machine_and_real_argument_wiring(self):
        from agent_hub_host import cli
        argv = ['hub', 'bridge-resume', '--actor', 'project:worker', '--project', 'project',
                '--workspace', self.bridge.workspace, '--machine-id', 'machine-one',
                '--conversation', 'c1', '--resume-after-seq', '2']
        for flags in ([], ['--enable-native'], ['--acknowledge-gap']):
            with patch('sys.argv', argv + flags), patch('sys.stderr', io.StringIO()), \
                 patch.object(cb.config, 'load_config') as cfg, self.assertRaises(SystemExit):
                cli.main()
            cfg.assert_not_called()
        with patch('sys.argv', argv + ['--enable-native', '--acknowledge-gap']), \
             patch('sys.stdout', io.StringIO()), patch.dict(os.environ, {'HUB_URL': 'http://localhost:8787'}), \
             patch.object(cb.config, 'load_config', return_value={'machine_id': 'machine-one'}), \
             patch.object(cb.ConversationBridge, 'resume', return_value={}) as resume:
            cli.main()
            resume.assert_called_once_with('c1', 2, acknowledge_gap=True)
