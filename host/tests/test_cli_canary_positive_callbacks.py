"""Pure canary positive-claim integration: no sockets, SMTP, subprocess or manager."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class CanaryPositiveCallbacksTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='positive-canary-', dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.canary, self.client = module('hub_start_canary'), module('hub_alert_client')
        self.frames, self.mail, self.timeline = [], [], []
        self.ack = True
        def exchange(path, request, *, deadline):
            self.client.checked(request['frame'])
            self.assertEqual(request['op'], 'hook')
            self.assertEqual(path, '/synthetic/observer.sock')
            self.frames.append(json.loads(json.dumps(request['frame'])))
            self.timeline.append(('frame', request['frame']['phase'], request['frame']['condition']))
            return {'seq': request['frame']['seq'], 'ok': self.ack}
        self.exchange = exchange
        for target, value in [('state_dir', lambda: self.directory), ('send_mail', self.send)]:
            guard = patch.object(self.canary, target, value)
            guard.start(); self.addCleanup(guard.stop)
        guard = patch.object(self.client, 'exchange', self.exchange)
        guard.start(); self.addCleanup(guard.stop)
        # No inherited context/config is adopted. Only synthetic owned selection.
        guard = patch.dict(os.environ, {}, clear=True)
        guard.start(); self.addCleanup(guard.stop)

    def send(self, subject, body):
        self.mail.append((subject, body))
        self.timeline.append(('mail',))
        self.assertEqual(self.frames[-1]['phase'], 'decision')
        self.assertIn(self.frames[-1]['decision'], ('alert', 'recovery'))
        return True

    def result(self, ok=False, **values):
        return {'mode':'shell', 'ok':ok, 'stage':None if ok else 'running',
                'reason':None if ok else 'synthetic_failure', 'timestamp':'synthetic-time',
                'run_id':'synthetic-run', 'cleanup':'not_needed', **values}

    def run_alert(self, result):
        observer = self.canary.PositiveCanaryObserver(self.client, '/synthetic/observer.sock')
        before = len(self.frames)
        self.canary.apply_alerting(result, observer=observer)
        frames = self.frames[before:]
        self.assertEqual([f['phase'] for f in frames], ['begin', 'decision', 'terminal'])
        self.assertEqual([f['seq'] for f in frames], [0, 1, 2])
        self.assertEqual(len({f['invocation_id'] for f in frames}), 1)
        self.assertIsNone(frames[0]['parent_id'])
        self.assertTrue(all(f['role'] == 'canary' for f in frames))
        return observer, frames

    def test_real_decisions_precede_mail_and_dedup_without_replay(self):
        conditions = []
        for result in [self.result(), self.result(), self.result(), self.result(True),
                       self.result(True), self.result(True),
                       self.result(stage='lock', reason='deploy_gate_lock_busy')]:
            _, frames = self.run_alert(result)
            conditions.append(frames[1]['condition'])
        self.assertEqual(conditions, ['failure_below_threshold','critical','critical_dedup',
                                     'recovering','recovery','pass','lock_busy'])
        self.assertEqual(len(self.mail), 2)
        self.assertEqual(sum(f['decision'] == 'alert' for f in self.frames), 1)
        self.assertEqual(sum(f['decision'] == 'alert_dedup' for f in self.frames), 1)
        for i, value in enumerate(self.timeline):
            if value[0] == 'mail': self.assertEqual(self.timeline[i-1][1], 'decision')
        self.assertEqual(len({f['invocation_id'] for f in self.frames}), 7)

    def test_negative_ack_is_sticky_while_original_mail_happens_once(self):
        self.ack = False
        first, _ = self.run_alert(self.result())
        second, frames = self.run_alert(self.result())
        self.assertEqual(len(self.mail), 1)
        self.assertTrue(first.dirty and second.dirty)
        self.assertEqual([f['observer_ok'] for f in frames], [True, False, False])
        self.ack = True
        self.assertFalse(second.emit('terminal'))
        self.assertTrue(second.dirty)
        self.assertFalse(self.frames[-1]['observer_ok'])
        self.assertEqual(len(self.mail), 1)

    def test_ordinary_transport_failure_does_not_skip_or_repeat_business(self):
        with patch.object(self.client, 'exchange', side_effect=OSError('private transport detail')):
            for result in [self.result(), self.result()]:
                observer = self.canary.PositiveCanaryObserver(self.client, '/synthetic/observer.sock')
                with patch.object(self.canary, 'send_mail', return_value=True) as mail:
                    self.canary.apply_alerting(result, observer=observer)
                self.assertTrue(observer.dirty)
        self.assertEqual(mail.call_count, 1)

    def test_default_off_never_constructs_context_and_preserves_alert_threshold(self):
        with patch.object(self.client, 'positive_only_context', side_effect=AssertionError('unexpected selection')):
            with patch.object(self.canary, 'send_mail', return_value=True) as mail:
                self.canary.apply_alerting(self.result())
                self.canary.apply_alerting(self.result())
                self.canary.apply_alerting(self.result())
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(self.frames, [])

    def test_missing_selection_remains_dirty_without_blocking_mail(self):
        observer = self.canary.PositiveCanaryObserver(None, '/synthetic/observer.sock')
        with patch.object(self.canary, 'send_mail', return_value=True) as mail:
            self.canary.apply_alerting(self.result(), observer=observer)
            self.canary.apply_alerting(self.result(), observer=observer)
        self.assertTrue(observer.dirty)
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(self.frames, [])

    def test_business_exception_identity_survives_terminal_interrupt(self):
        original = OSError('private business error')
        observer = self.canary.PositiveCanaryObserver(self.client, '/synthetic/observer.sock')
        emit = observer.emit
        def terminal_interrupt(phase, **fields):
            if phase == 'terminal': raise KeyboardInterrupt('private callback interruption')
            return emit(phase, **fields)
        with patch.object(self.canary, 'load_state', side_effect=original), patch.object(observer, 'emit', terminal_interrupt):
            with self.assertRaises(OSError) as caught:
                self.canary.apply_alerting(self.result(), observer=observer)
        self.assertIs(caught.exception, original)
        self.assertTrue(observer.dirty)

    def test_control_interrupt_latches_client_and_propagates_before_business(self):
        observer = self.canary.PositiveCanaryObserver(self.client, '/synthetic/observer.sock')
        original = KeyboardInterrupt('synthetic stop')
        with patch.object(self.client, 'exchange', side_effect=original), patch.object(self.canary, '_apply_alerting') as business:
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.canary.apply_alerting(self.result(), observer=observer)
        self.assertIs(caught.exception, original)
        self.assertTrue(observer.dirty and observer.child.dirty)
        business.assert_not_called()

    def test_fixed_projection_never_sends_raw_result_state_or_paths(self):
        observer = self.canary.PositiveCanaryObserver(self.client, '/synthetic/observer.sock')
        self.canary.record_dev_alert_occurrence(Path('/private/source'), 'private-mode',
            {'reason':'secret result'}, {'token':'secret state'}, decision='critical', observer=observer)
        raw = json.dumps(self.frames)
        for value in ['private/source','private-mode','secret result','secret state']:
            self.assertNotIn(value, raw)
        self.assertEqual(self.frames[0]['flags'], {})
        self.assertEqual(self.frames[0]['recipient_class'], 'dev')
        self.assertFalse(self.canary.record_dev_alert_occurrence(None,None,None,None,decision={'bad':'value'},observer=observer))
        self.assertTrue(observer.dirty)

    def test_shared_factory_keeps_quota_abi_and_rejects_inherited_or_backup_context(self):
        for role in ['quota_publish', 'quota_monitor', 'canary']:
            child = self.client.positive_only_context(role, socket_path='/synthetic/observer.sock')
            self.assertEqual(child.role, role)
            self.assertRegex(child.identity, r'^[0-9a-f]{32}$')
        for role in ['backup_pull','backup_notifier','unknown']:
            with self.assertRaises(self.client.Refused):
                self.client.positive_only_context(role, socket_path='/synthetic/observer.sock')
        with patch.dict(os.environ, {'HUB_ALERT_INVOCATION_ID':'a'*32}):
            with self.assertRaises(self.client.Refused):
                self.client.positive_only_context('canary', socket_path='/synthetic/observer.sock')

    def test_each_exchange_keeps_existing_total_budget_and_explicit_deadline_clip(self):
        observer = self.canary.PositiveCanaryObserver(self.client, '/synthetic/observer.sock', deadline=42.05)
        deadlines = []
        def exchange(path, request, *, deadline):
            deadlines.append(deadline)
            return {'seq':request['frame']['seq'],'ok':True}
        with patch.object(self.client.time, 'monotonic', return_value=42), patch.object(self.client,'exchange',exchange):
            observer.emit('begin')
        self.assertEqual(deadlines, [42.05])
        self.assertEqual(self.client.BUDGET, 0.15)


if __name__ == '__main__':
    unittest.main()
