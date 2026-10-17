import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agent_hub_host import conversation_bridge as cb
from agent_hub_host.native_bridge import BridgeCapabilityGap, NativeServiceBinding


class Native:
    """Fake installed contract: bound whoami, tag routing, durable log, no panes."""
    def __init__(self, workspace, tag='worker'):
        self.binding = NativeServiceBinding(str(uuid.uuid4()), tag, workspace, 'codex')
        self.messages, self.sends = [], 0
        self.effect_then_error = self.error_without_effect = False

    def qualify(self, workspace):
        if workspace != self.binding.workspace:
            raise BridgeCapabilityGap('workspace mismatch')
        return self.binding

    def log(self, workspace):
        return copy.deepcopy(self.messages)

    def envelope(self, body, kind='reply', reply_to=None, data=None):
        b = self.binding
        return dict(id=str(uuid.uuid4()), workspace=b.workspace,
                    **{'from': dict(tag=b.tag, session_id=b.session_id, workspace=b.workspace)},
                    to=dict(tag=b.tag, session_id=b.session_id), delivery='inbox',
                    kind=kind, body=body, reply_to=reply_to, data=data)

    def send(self, binding, body, data):
        self.sends += 1
        if self.error_without_effect:
            raise subprocess.TimeoutExpired('native', 30)
        row = self.envelope(body, 'hub-conversation', data=data)
        self.messages.append(row)
        if self.effect_then_error:
            raise subprocess.CalledProcessError(1, 'native')
        return row

    def reply(self, body='reply', **changes):
        row = self.envelope(body, reply_to=self.messages[0]['id'])
        row.update(changes)
        self.messages.append(row)
        return row


class Hub:
    def __init__(self):
        self.actors, self.messages, self.sent, self.reads = {}, [], {}, {}
        self.removed = set()
        self.read_error = self.send_error = False
        self.posts = []

    def client(self, actor, project, machine, native):
        def request(method, path, payload=None):
            self.posts.append((actor, method, path, copy.deepcopy(payload)))
            if path == '/api/conversations/participants':
                return {'self': {'actor': actor, 'kind': 'agent'},
                        'participants': list(self.actors.values())}
            cid = path.split('/')[3]
            if actor in self.removed:
                raise BridgeCapabilityGap('removed member')
            if path == '/api/conversations/' + cid:
                return {'self_actor': actor, 'members': [{'actor': a, 'removed_at': None}
                                                        for a in self.actors if a not in self.removed]}
            if method == 'GET' and '/messages?' in path:
                cursor = int(path.split('after_seq=')[1].split('&')[0])
                return dict(self_actor=actor, conversation_id=cid,
                            messages=copy.deepcopy([m for m in self.messages if m['seq'] > cursor and m['conversation_id'] == cid]))
            if path.endswith('/read'):
                key = actor, cid
                self.reads[key] = max(self.reads.get(key, 0), payload['seq'])
                if self.read_error:
                    raise OSError('uncertain HTTP read')
                return dict(actor=actor, conversation_id=cid, read_seq=self.reads[key])
            if path.endswith('/messages') and method == 'POST':
                key = actor, cid, payload['idempotency_key']
                if key in self.sent and any(self.sent[key].get(k) != v for k, v in payload.items()):
                    raise ValueError('idempotency conflict')
                if key not in self.sent:
                    row = dict(payload, sender_actor=actor, conversation_id=cid,
                               id='hub-reply-' + str(len(self.sent)), seq=len(self.messages) + 1)
                    self.sent[key] = row
                    self.messages.append(row)
                if self.send_error:
                    raise OSError('uncertain HTTP send')
                return copy.deepcopy(self.sent[key])
            raise AssertionError((method, path))
        return request

    def message(self, seq=1, cid='c1', sender='human:one', body='hello'):
        row = dict(id='m' + str(seq), seq=seq, conversation_id=cid, sender_actor=sender, body=body)
        self.messages.append(row)
        return row


class TestConversationBridge(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / '.local' / 'tmp'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.hub = Hub()
        self.bridge, self.native = self.client('project', 'one')

    def client(self, project, name):
        workspace = str(Path(self.tmp.name) / name)
        Path(workspace).mkdir()
        native = Native(workspace)
        actor, machine = project + ':worker', 'machine-' + name
        self.hub.actors[actor] = dict(actor=actor, kind='agent', project=project,
                                     machine_id=machine, native_binding=dict(tag='worker', project=project, machine_id=machine))
        return cb.ConversationBridge(actor=actor, project=project, workspace=workspace,
                                     machine_id=machine, conversation_ids=['c1'], origin='http://localhost:8787',
                                     native=native, request=self.hub.client(actor, project, machine, native)), native

    def mutations(self):
        return [r for r in self.hub.posts if r[1] == 'POST']

    def test_opt_in_required_before_any_effect(self):
        with patch('subprocess.run') as native, patch.object(cb.conversations, 'request') as http:
            with self.assertRaises(BridgeCapabilityGap):
                cb.run_cycles()
            native.assert_not_called()
            http.assert_not_called()

    def test_bounded_cycles(self):
        for value in (0, 1001, -1, float('nan'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cb.run_cycles(value)

    def test_two_independently_bound_clients_same_tag_separate_projects(self):
        other, native = self.client('other-project', 'two')
        self.hub.message()
        self.bridge.cycle()
        other.cycle()
        self.native.reply('first participant')
        native.reply('second participant')
        self.bridge.cycle()
        other.cycle()
        self.assertEqual({r['sender_actor'] for r in self.hub.sent.values()}, {'project:worker', 'other-project:worker'})
        self.assertEqual(len(self.hub.sent), 2)
        self.assertNotEqual(self.native.binding.session_id, native.binding.session_id)
        self.assertTrue(all(r['reply_to'] == 'm1' for r in self.hub.sent.values()))

    def test_reconnect_dedup_and_self_messages_do_not_loop(self):
        self.hub.message()
        self.bridge.cycle()
        self.native.reply()
        self.bridge.cycle()
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertEqual(len(self.hub.sent), 1)
        self.assertEqual(self.hub.reads[('project:worker', 'c1')], 2)

    def test_effect_then_native_error_reconciles_authoritative_log(self):
        self.hub.message()
        self.native.effect_then_error = True
        self.bridge.cycle()
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertEqual(self.hub.reads[('project:worker', 'c1')], 1)

    def test_unknown_native_effect_never_blind_resend_or_read(self):
        self.hub.message()
        self.native.error_without_effect = True
        for _ in range(2):
            with self.assertRaisesRegex(BridgeCapabilityGap, 'uncertain'):
                self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertFalse(self.hub.reads)
        state = json.loads(self.bridge.path.read_text())
        self.assertTrue(state['deliveries']['c1:1']['attempted'])
        self.assertIsNone(state['deliveries']['c1:1']['native_id'])

    def test_late_durable_effect_can_reconcile_after_restart(self):
        self.hub.message()
        self.native.error_without_effect = True
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        row = json.loads(self.bridge.path.read_text())['deliveries']['c1:1']
        self.native.messages.append(self.native.envelope(row['body'], 'hub-conversation', data=row['data']))
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertEqual(self.hub.reads[('project:worker', 'c1')], 1)

    def test_membership_removed_after_durable_send_prevents_read(self):
        self.hub.message()
        send = self.native.send
        def remove(*args):
            result = send(*args)
            self.hub.removed.add('project:worker')
            return result
        self.native.send = remove
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertFalse(self.hub.reads)
        self.assertIsNotNone(json.loads(self.bridge.path.read_text())['deliveries']['c1:1']['native_id'])

    def test_duplicate_matching_native_effects_prevent_read(self):
        self.hub.message()
        send = self.native.send
        def duplicate(*args):
            result = send(*args)
            row = copy.deepcopy(result)
            row['id'] = str(uuid.uuid4())
            self.native.messages.append(row)
            return result
        self.native.send = duplicate
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertFalse(self.hub.reads)

    def test_removed_membership_has_no_effect(self):
        self.hub.message()
        self.hub.removed.add('project:worker')
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertFalse(self.mutations())
        self.assertEqual(self.native.sends, 0)

    def test_membership_removed_after_history_before_delivery(self):
        self.hub.message()
        request = self.bridge.request
        def remove(method, path, payload=None):
            result = request(method, path, payload)
            if '/messages?' in path:
                self.hub.removed.add('project:worker')
            return result
        self.bridge.request = remove
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertEqual(self.native.sends, 0)
        self.assertFalse(self.mutations())

    def test_credential_and_registered_machine_mismatch(self):
        self.hub.message()
        for field, value in [('actor', 'other:worker'), ('project', 'different'), ('machine_id', 'foreign')]:
            saved = copy.deepcopy(self.hub.actors['project:worker'])
            self.hub.actors['project:worker'][field] = value
            with self.subTest(field=field), self.assertRaises(BridgeCapabilityGap):
                self.bridge.cycle()
            self.hub.actors['project:worker'] = saved
        self.assertEqual(self.native.sends, 0)

    def test_native_send_wrong_uuid_prevents_read(self):
        self.hub.message()
        send = self.native.send
        def bad(*args):
            r = send(*args)
            r['from']['session_id'] = str(uuid.uuid4())
            return r
        self.native.send = bad
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertFalse(self.hub.reads)

    def test_forged_native_replies_and_unrelated_mapping_ignored(self):
        self.hub.message()
        self.bridge.cycle()
        original = self.native.messages[0]['id']
        for change in [{'from': {'tag': 'worker', 'workspace': self.bridge.workspace, 'session_id': str(uuid.uuid4())}},
                       {'workspace': '/foreign'}, {'to': {'tag': 'worker', 'session_id': 'foreign'}},
                       {'reply_to': 'unrelated'}, {'kind': 'hub-conversation'}, {'delivery': 'pane'}]:
            self.native.reply(**change)
        self.bridge.cycle()
        self.assertFalse(self.hub.sent)
        self.assertEqual(self.native.messages[0]['id'], original)

    def test_reply_mapping_pinned_to_delivered_conversation(self):
        self.bridge.ids.append('c2')
        self.hub.message(cid='c1')
        self.hub.message(cid='c2')
        self.bridge.cycle()
        self.native.reply('c2 reply', reply_to=self.native.messages[1]['id'])
        self.bridge.cycle()
        self.assertEqual(next(iter(self.hub.sent.values()))['conversation_id'], 'c2')

    def test_http_read_uncertainty_does_not_advance_local_cursor_or_redeliver(self):
        self.hub.message()
        self.hub.read_error = True
        with self.assertRaises(OSError):
            self.bridge.cycle()
        self.assertEqual(json.loads(self.bridge.path.read_text())['cursors'], {})
        self.hub.read_error = False
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertEqual(json.loads(self.bridge.path.read_text())['cursors'], {'c1': 1})

    def test_reply_http_uncertainty_reuses_stable_key(self):
        self.hub.message()
        self.bridge.cycle()
        self.native.reply()
        self.hub.send_error = True
        with self.assertRaises(OSError):
            self.bridge.cycle()
        self.hub.send_error = False
        self.bridge.cycle()
        self.assertEqual(len(self.hub.sent), 1)
        posts = [p[3] for p in self.hub.posts if p[1] == 'POST' and p[2].endswith('/messages')]
        self.assertEqual(posts[0], posts[1])

    def test_durable_reply_outbox_retries_after_native_log_retention(self):
        self.hub.message()
        self.bridge.cycle()
        self.native.reply()
        self.hub.send_error = True
        with self.assertRaises(OSError):
            self.bridge.cycle()
        self.hub.send_error = False
        self.native.messages.clear()
        self.bridge.cycle()
        self.assertEqual(len(self.hub.sent), 1)
        rows = json.loads(self.bridge.path.read_text())['replies'].values()
        self.assertTrue(all(row['done'] for row in rows))

    def test_retry_conflicting_native_payload_fails_closed(self):
        self.hub.message()
        self.bridge.cycle()
        reply = self.native.reply()
        self.hub.send_error = True
        with self.assertRaises(OSError):
            self.bridge.cycle()
        reply['body'] = 'changed after durable intent'
        self.hub.send_error = False
        with self.assertRaisesRegex(BridgeCapabilityGap, 'conflict'):
            self.bridge.cycle()
        self.assertEqual(len(self.hub.sent), 1)

    def test_delivery_payload_conflict_after_read_failure(self):
        row = self.hub.message()
        self.hub.read_error = True
        with self.assertRaises(OSError):
            self.bridge.cycle()
        row['body'] = 'changed'
        self.hub.read_error = False
        with self.assertRaisesRegex(BridgeCapabilityGap, 'conflict'):
            self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)

    def test_session_rollover_rejected_by_durable_binding(self):
        self.hub.message()
        self.bridge.cycle()
        self.native.binding = NativeServiceBinding(str(uuid.uuid4()), 'worker', self.bridge.workspace)
        with self.assertRaisesRegex(BridgeCapabilityGap, 'binding changed'):
            self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)

    def test_atomic_journal_and_exclusive_cycle_lock(self):
        self.hub.message()
        with self.bridge.journal():
            with self.assertRaises(BlockingIOError):
                self.bridge.cycle()
        self.assertEqual(self.native.sends, 0)
        self.assertEqual(self.bridge.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(str(self.bridge.path) + '.tmp').exists())

    def test_symlink_journal_rejected(self):
        self.bridge.path.parent.mkdir()
        destination = Path(self.tmp.name) / 'foreign'
        destination.write_text('private')
        self.bridge.path.symlink_to(destination)
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertEqual(destination.read_text(), 'private')

    def test_history_gap_and_wrong_conversation_do_not_read(self):
        self.hub.message(seq=2)
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertFalse(self.hub.reads)
        self.assertEqual(self.native.sends, 0)

    def test_pending_intent_is_durable_before_each_transport_effect(self):
        self.hub.message()
        send = self.native.send
        def checked_send(*args):
            row = json.loads(self.bridge.path.read_text())['deliveries']['c1:1']
            self.assertTrue(row['attempted'])
            self.assertEqual(row['body'], args[1])
            self.assertIsNone(row['native_id'])
            return send(*args)
        self.native.send = checked_send
        self.bridge.cycle()
        reply = self.native.reply()
        request = self.bridge.request
        def checked_request(method, path, payload=None):
            if method == 'POST' and path.endswith('/messages'):
                pending = json.loads(self.bridge.path.read_text())['replies'][reply['id']]
                self.assertFalse(pending['done'])
                self.assertEqual(pending['payload'], payload)
            return request(method, path, payload)
        self.bridge.request = checked_request
        self.bridge.cycle()
        self.assertEqual(len(self.hub.sent), 1)

    def test_crash_before_mapping_save_recovers_log_without_duplicate(self):
        self.hub.message()
        save = self.bridge.save
        def crash():
            if self.bridge.state['deliveries'].get('c1:1', {}).get('native_id'):
                raise OSError('crash before mapping fsync')
            save()
        with patch.object(self.bridge, 'save', side_effect=crash):
            with self.assertRaises(OSError):
                self.bridge.cycle()
        self.assertFalse(self.hub.reads)
        self.assertEqual(self.native.sends, 1)
        self.bridge.cycle()
        self.assertEqual(self.native.sends, 1)
        self.assertEqual(self.hub.reads[('project:worker', 'c1')], 1)

    def test_removed_member_cannot_relay_already_delivered_reply(self):
        self.hub.message()
        self.bridge.cycle()
        self.native.reply()
        self.hub.removed.add('project:worker')
        with self.assertRaises(BridgeCapabilityGap):
            self.bridge.cycle()
        self.assertFalse(self.hub.sent)

    def test_human_or_other_authenticated_self_cannot_use_agent_binding(self):
        request = self.bridge.request
        for identity in ({'actor': 'human:one', 'kind': 'human'},
                         {'actor': 'other-project:worker', 'kind': 'agent'}):
            def other(method, path, payload=None):
                result = request(method, path, payload)
                if path.endswith('/participants'):
                    result['self'] = identity
                return result
            self.bridge.request = other
            with self.assertRaises(BridgeCapabilityGap):
                self.bridge.cycle()
        self.assertEqual(self.native.sends, 0)
        self.assertFalse(self.mutations())

    def test_cli_opt_in_wires_explicit_mapping_and_existing_config(self):
        import io
        from agent_hub_host import cli
        argv = ['hub', 'conversations', 'bridge', '--enable-native', '--once',
                '--actor', 'project:worker', '--project', 'project',
                '--workspace', self.bridge.workspace, '--machine-id', 'machine-one', '--conversation', 'c1']
        with patch('sys.argv', argv), patch('sys.stdout', io.StringIO()), \
             patch.dict(os.environ, {'HUB_URL': 'http://localhost:8787'}), \
             patch.object(cb.config, 'load_config', return_value={'machine_id': 'machine-one'}), \
             patch.object(cb, 'run_cycles', return_value={'cycles': []}) as run:
            cli.main()
            self.assertEqual(run.call_args.args, (1,))
            self.assertEqual(run.call_args.kwargs['conversation_ids'], ['c1'])
            self.assertEqual(run.call_args.kwargs['actor'], 'project:worker')
            self.assertTrue(run.call_args.kwargs['enabled'])

    def test_transport_uses_tag_self_route_no_env_override_or_pane(self):
        native = cb.NativeMailbox('/installed/aplexer')
        with patch('subprocess.run') as run:
            run.return_value.stdout = '{}'
            native.send(self.native.binding, '-nonce', {'intent': 'nonce'})
            args, kwargs = run.call_args
            argv = args[0]
            self.assertEqual(argv[1:4], ['--json', 'message', 'send'])
            self.assertEqual(argv[argv.index('--to') + 1], 'worker')
            self.assertEqual(argv[-2:], ['--', '-nonce'])
            self.assertFalse(any(flag in argv for flag in ('--pane', '--from', '--queue', '--enter')))
            self.assertNotIn('env', kwargs)

    def test_installed_whoami_phase_and_catalog_contract(self):
        native = cb.NativeMailbox()
        binding = self.native.binding
        own = dict(id=binding.session_id, workspace=binding.workspace, tag=binding.tag, phase='running')
        with patch.object(native, 'call', side_effect=[own, [own]]), patch.object(cb, 'qualify_native_service_binding', return_value=binding):
            self.assertEqual(native.qualify(binding.workspace), binding)
        with patch.object(native, 'call', side_effect=[own, [own, own]]):
            with self.assertRaises(BridgeCapabilityGap):
                native.qualify(binding.workspace)


if __name__ == '__main__':
    unittest.main()
