import importlib.util
import json
from pathlib import Path
import tempfile
import types
import shutil
import subprocess
import sys
import importlib
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('intake_recovery_alerts', REPO / 'scripts/hub-intake-recovery-alerts.py')
poller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(poller)

ALERT = {'recipient': poller.RECIPIENT, 'kind': 'recovery_exhausted', 'reason': 'exit_without_completion_claim',
         'session_id': 's-3', 'depth': 2, 'max_continuations': 2}


class StubTransport:
    def __init__(self, owner=poller.RECIPIENT, machines=None):
        self.owner = owner
        self.machines = machines or []
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if path == '/api/fleet/routing/machines':
            if self.machines == 'fail':
                raise OSError('routing view unavailable')
            return {'machines': self.machines}
        if path.startswith('/api/intakes?'):
            if 'status=failed' in path:
                return {'items': [{'id': 'abc-1', 'project': 'o/r', 'title': 'T', 'status': 'failed'}], 'next': None}
            return {'items': [], 'next': None}
        if path == '/api/intakes/abc-1/context':
            return {'task': {'owner': self.owner}, 'events': [
                {'seq': 7, 'kind': 'worker_append', 'body': {}},
                {'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}]}
        assert path == '/api/intakes/abc-1/history?after=0&limit=100', path
        return {'events': [
            {'seq': 7, 'kind': 'worker_append', 'body': {}},
            {'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}], 'next': None}


class RecoveryAlertTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.state = root / 'state/sent.json'
        self.log = root / 'mail.log'
        self.helper = root / 'helper.sh'
        self.helper.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$PRINCIPAL_ALERT_TO" "$PING_ALERT_MESSAGE" >> ' + str(self.log) + '\n')
        self.helper.chmod(0o700)
        self.helper_returncode = 0
        self.helper_calls = []
        # No test can execute a mail helper/MTA. Use a wholly controlled child
        # environment rather than removing an inherited sink before real mail.
        env_patch = patch.object(poller.os, 'environ', {})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        helper_patch = patch.object(poller.subprocess, 'run', side_effect=self.run_helper)
        helper_patch.start()
        self.addCleanup(helper_patch.stop)

    def run_helper(self, argv, *, env, **kwargs):
        self.assertEqual(argv[0], str(self.helper), 'only the synthetic helper is permitted')
        self.helper_calls.append((argv, dict(env)))
        if self.helper_returncode in (0, 75):
            target = Path(env['ALERT_LOG']) if env.get('ALERT_LOG') else self.log
            with target.open('a') as stream:
                stream.write(env['PRINCIPAL_ALERT_TO'] + '|' + env['PING_ALERT_MESSAGE'] + '\n')
        return types.SimpleNamespace(returncode=self.helper_returncode)

    def tearDown(self):
        self.dir.cleanup()

    def test_mails_developer_once_per_alert(self):
        transport = StubTransport()
        self.assertEqual(poller.deliver(transport, self.state, self.helper), {'sent': 1, 'errors': 0})
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        to, message = lines[0].split('|', 1)
        self.assertEqual(to, 'dev@dtcdev.click')
        self.assertIn('abc-1', message)
        self.assertIn('recovery_exhausted', message)
        self.assertIn('search=dev%40dtcdev.click', transport.paths[0])
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 0, 'errors': 0})
        self.assertEqual(len(self.log.read_text().splitlines()), 1, 'an alert is mailed once')
        self.assertEqual(json.loads(self.state.read_text())['sent'], ['abc-1:9'])

    def test_alert_names_the_cause_not_only_the_kind(self):
        alert = dict(ALERT, kind='blocked', reason='blocked_permission',
                     cause='blocked (permission): hub model-policy returns HTTP 403 for this worker\nNext: Root applies the policy')
        message = poller.render({'id': 'abc-1', 'project': 'o/r', 'title': 'T', 'status': 'failed'}, alert)
        self.assertIn('Alert blocked (blocked_permission)', message)
        self.assertIn('Cause: blocked (permission): hub model-policy returns HTTP 403 for this worker Next: Root', message)
        self.assertNotIn('Cause:', poller.render({'id': 'abc-1'}, ALERT), 'older alerts without a cause still render')

    def test_skips_intakes_someone_already_owns(self):
        self.assertEqual(poller.deliver(StubTransport(owner='human:x'), self.state, self.helper), {'sent': 0, 'errors': 0})
        self.assertFalse(self.log.exists())

    def test_failed_helper_is_retried_next_run(self):
        self.helper.write_text('#!/bin/sh\nexit 3\n')
        self.helper_returncode = 3
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 0, 'errors': 1})
        self.assertFalse(self.state.exists())
        self.helper_returncode = 0
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 1, 'errors': 0})

    def test_successful_log_path_override_still_journals_delivery(self):
        normal_log = Path(self.dir.name) / 'normal-mail-log'
        poller.os.environ['ALERT_LOG'] = str(normal_log)
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 1, 'errors': 0})
        self.assertTrue(normal_log.exists())
        self.assertEqual(json.loads(self.state.read_text())['sent'], ['abc-1:9'])
        self.assertEqual(self.helper_calls[0][1]['ALERT_LOG'], str(normal_log))
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 0, 'errors': 0})
        self.assertEqual(len(self.helper_calls), 1)

    def test_nonzero_render_receipt_never_suppresses_later_delivery(self):
        sink = Path(self.dir.name) / 'render-only'
        poller.os.environ['ALERT_LOG'] = str(sink)
        self.helper_returncode = 75
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 0, 'errors': 1})
        self.assertTrue(sink.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.helper_calls[0][1]['ALERT_LOG'], str(sink))
        self.helper_returncode = 0  # Pure fake ordinary helper with log-path semantics.
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 1, 'errors': 0})
        self.assertEqual(len(self.helper_calls), 2)
        self.assertEqual(json.loads(self.state.read_text())['sent'], ['abc-1:9'])

    def test_host_render_failure_keeps_prior_receipts_then_ordinary_log_can_send(self):
        sink = Path(self.dir.name) / 'host-log'
        poller.os.environ['ALERT_LOG'] = str(sink)
        poller.save_state(self.state, {'version': 1, 'sent': ['prior-delivery']})
        previous = self.state.read_bytes()
        self.helper_returncode = 75
        transport = StubTransport(owner='human:x', machines=[HOST])
        self.assertEqual(poller.deliver(transport, self.state, self.helper), {'sent': 0, 'errors': 1})
        self.assertEqual(self.state.read_bytes(), previous)
        self.helper_returncode = 0
        self.assertEqual(poller.deliver(transport, self.state, self.helper), {'sent': 1, 'errors': 0})
        self.assertEqual(len(json.loads(self.state.read_text())['sent']), 2)
HOST = {'machine_id': 'DESKTOP-FP4OP26', 'connected': False, 'last_seen': 1_800_000_900_000,
        'host_alert': {'kind': 'host_disconnected', 'recipient': 'dev@dtcdev.click',
                       'disconnected_since': 1_800_000_000_000, 'alerted_at': 1_800_000_960_000,
                       'evidence': 'http_heartbeat_without_socket'}}


class HostDisconnectAlertTest(RecoveryAlertTest):
    def test_mails_developer_once_per_disconnect_episode(self):
        machines = [HOST, {'machine_id': 'quiet', 'connected': True},
                    dict(HOST, machine_id='reconnected', connected=True)]
        self.assertEqual(poller.deliver(StubTransport(owner='human:x', machines=machines), self.state, self.helper),
                         {'sent': 1, 'errors': 0})
        to, message = self.log.read_text().splitlines()[0].split('|', 1)
        self.assertEqual(to, 'dev@dtcdev.click')
        self.assertIn('DESKTOP-FP4OP26 is running but disconnected since 2027-01-15', message)
        self.assertIn('http_heartbeat_without_socket', message)
        self.assertIn('AgentHub2Host', message)
        self.assertEqual(poller.deliver(StubTransport(owner='human:x', machines=machines), self.state, self.helper),
                         {'sent': 0, 'errors': 0})
        later = dict(HOST, host_alert=dict(HOST['host_alert'], disconnected_since=1_800_100_000_000))
        self.assertEqual(poller.deliver(StubTransport(owner='human:x', machines=[later]), self.state, self.helper),
                         {'sent': 1, 'errors': 0}, 'a new disconnect episode alerts again')

    def test_host_view_failure_keeps_intake_alerts_and_fails_the_run(self):
        self.assertEqual(poller.deliver(StubTransport(machines='fail'), self.state, self.helper), {'sent': 1, 'errors': 1})


class CopiedGuardHelperContractTest(unittest.TestCase):
    """Only a copied public helper, private HOME/PATH and a non-network fake MTA."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.helper = self.home / 'copied-principal-alert.sh'
        self.helper.write_bytes((REPO / 'ops/guard-bin/principal-alert.sh').read_bytes())
        self.helper.chmod(0o700)
        for name in ('dirname', 'mkdir'):
            (self.bin / name).symlink_to('/usr/bin/' + name)
        for name, value in (('date', '2000-01-01T00:00:00Z'), ('hostname', 'synthetic-test-host')):
            tool = self.bin / name
            tool.write_text('#!/bin/sh\nprintf "%s\\n" "' + value + '"\n')
            tool.chmod(0o700)
        self.mta_log = self.home / 'fake-mta-calls'
        self.mta = self.bin / 'msmtp'
        self.mta.write_text('#!/bin/sh\nprintf "called\\n" >> "$FAKE_MTA_LOG"\n/bin/cat >/dev/null\nexit "${FAKE_MTA_EXIT:-0}"\n')
        self.mta.chmod(0o700)
        self.env = {'HOME': str(self.home), 'PATH': str(self.bin), 'LANG': 'C.UTF-8',
                    'PRINCIPAL_ALERT_TO': 'nobody@invalid.test',
                    'PRINCIPAL_ALERT_LOG': str(self.home / 'local-log'),
                    'FAKE_MTA_LOG': str(self.mta_log)}

    def invoke(self, extra=None, *, missing_mta=False):
        self.assertEqual(self.mta.is_file(), not missing_mta, 'only the isolated PATH is used')
        return subprocess.run(['/bin/bash', str(self.helper), 'synthetic-test-only'],
                              env=dict(self.env, **(extra or {})), capture_output=True, timeout=5)

    def test_render_only_returns75_without_any_fake_smtp_call(self):
        sink = self.home / 'render-log'
        result = self.invoke({'ALERT_LOG': str(sink)})
        self.assertEqual(result.returncode, 75)
        self.assertIn('synthetic-test-only', sink.read_text())
        self.assertFalse(self.mta_log.exists(), 'render-only must not fall through to SMTP')

    def test_ordinary_fake_smtp_success_still_returns0(self):
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.invoke({'ALERT_LOG': ''}).returncode, 0)
        self.assertEqual(self.mta_log.read_text().splitlines(), ['called', 'called'])

    def test_fake_smtp_failure_still_returns_nonzero(self):
        self.assertEqual(self.invoke({'FAKE_MTA_EXIT': '3'}).returncode, 1)
        self.assertEqual(self.mta_log.read_text().splitlines(), ['called'])

    def test_missing_fake_mta_still_returns_nonzero_without_lookup_fallback(self):
        self.mta.unlink()  # Only this test's newly-created fake file.
        self.assertEqual(self.invoke(missing_mta=True).returncode, 1)
        self.assertFalse(self.mta_log.exists())


class HookRecorder:
    def __init__(self, fail=False):
        self.frames, self.dirty, self.fail_emit = [], False, fail
    def fail(self):
        self.dirty = True
    def emit(self, phase, **fields):
        self.frames.append(dict(phase=phase, **fields))
        if self.fail_emit:
            raise OSError('SECRET callback failure')
        return True


class PositiveHooksTest(RecoveryAlertTest):
    def test_both_branches_before_helper_and_journal_dedup(self):
        observer = HookRecorder()
        original = self.run_helper
        def helper(argv, **kwargs):
            self.assertEqual(observer.frames[-1]['decision'], 'alert')
            return original(argv, **kwargs)
        with patch.object(poller.subprocess, 'run', side_effect=helper):
            result = poller.deliver(StubTransport(machines=[HOST]), self.state, self.helper, observer=observer)
        self.assertEqual(result, {'sent': 2, 'errors': 0})
        decisions = [f for f in observer.frames if f.get('decision') == 'alert']
        self.assertEqual([f['condition'] for f in decisions], ['intake_recovery', 'host_disconnected'])
        again = HookRecorder()
        self.assertEqual(poller.deliver(StubTransport(machines=[HOST]), self.state, self.helper,
                                      observer=again), {'sent': 0, 'errors': 0})
        self.assertEqual([f['condition'] for f in again.frames if f.get('decision') == 'alert_dedup'],
                         ['intake_recovery', 'host_disconnected'])
        self.assertEqual(len(self.helper_calls), 2)

    def test_begin_before_token_journal_and_api(self):
        observer = HookRecorder()
        config = Path(self.dir.name) / 'synthetic-cli.json'
        config.write_text(json.dumps({'access_token': 'SYNTHETIC_ONLY'}))
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertEqual(observer.frames[0]['phase'], 'begin')
            return original(path, *args, **kwargs)
        transport = StubTransport(owner='human:x')
        with patch.object(Path, 'read_text', read), patch.object(poller, 'Transport', return_value=transport):
            self.assertEqual(poller.main(['--cli-json', str(config), '--helper', str(self.helper),
                                         '--state', str(self.state)], observer=observer), 0)
        self.assertEqual([f['phase'] for f in observer.frames], ['begin', 'decision', 'terminal'])
        self.assertEqual(observer.frames[1]['condition'], 'mailer_scan_empty')

    def test_default_off_never_loads_client(self):
        config = Path(self.dir.name) / 'synthetic-cli.json'
        config.write_text(json.dumps({'access_token': 'SYNTHETIC_ONLY'}))
        with patch.object(poller, 'AlertObserver', side_effect=AssertionError('observer activated')), \
             patch.object(poller, 'Transport', return_value=StubTransport(owner='human:x')):
            self.assertEqual(poller.main(['--cli-json', str(config), '--helper', str(self.helper),
                                         '--state', str(self.state)]), 0)

    def test_input_failure_is_original_and_dirty_with_terminal(self):
        observer = HookRecorder()
        original = KeyboardInterrupt('ORIGINAL_CONTROL')
        with patch.object(Path, 'read_text', side_effect=original):
            with self.assertRaises(KeyboardInterrupt) as caught:
                poller.main(['--cli-json', '/synthetic-only', '--helper', '/synthetic-helper'], observer=observer)
        self.assertIs(caught.exception, original)
        self.assertTrue(observer.dirty)
        self.assertEqual(observer.frames[-1]['phase'], 'terminal')
        self.assertFalse(observer.frames[-1]['flags']['exit_success'])

    def test_observer_failure_keeps_business_once(self):
        observer = HookRecorder(fail=True)
        self.assertEqual(poller.deliver(StubTransport(machines=[HOST]), self.state, self.helper,
                                      observer=observer), {'sent': 2, 'errors': 0})
        self.assertEqual(len(self.helper_calls), 2)
        self.assertTrue(observer.dirty)

    def test_partial_host_failure_not_empty_or_complete(self):
        observer = HookRecorder()
        self.assertEqual(poller.deliver(StubTransport(machines='fail'), self.state, self.helper,
                                      observer=observer), {'sent': 1, 'errors': 1})
        self.assertTrue(observer.dirty)
        conditions = [f.get('condition') for f in observer.frames]
        self.assertIn('mailer_api_error', conditions)
        self.assertNotIn('mailer_scan_empty', conditions)
        self.assertNotIn('mailer_scan_complete', conditions)

    def test_page_limit_is_incomplete_without_changing_business_result(self):
        class FullPages(StubTransport):
            def get(self, path):
                if path.startswith('/api/intakes?'):
                    self.paths.append(path)
                    return {'items': [], 'next': 'MORE_PAGES'}
                return super().get(path)
        observer = HookRecorder()
        transport = FullPages()
        self.assertEqual(poller.deliver(transport, self.state, self.helper, observer=observer),
                         {'sent': 0, 'errors': 0})
        self.assertEqual(sum(p.startswith('/api/intakes?') for p in transport.paths), 2*poller.MAX_PAGES)
        self.assertTrue(observer.dirty)
        self.assertIn('mailer_scan_incomplete', [f.get('condition') for f in observer.frames])
        self.assertNotIn('mailer_scan_empty', [f.get('condition') for f in observer.frames])
        self.assertEqual(self.helper_calls, [])

    def test_render_state_and_journal_errors_preserve_identity(self):
        for target, stage in [('render','mailer_render_error'), ('load_state','mailer_state_error'),
                              ('save_state','mailer_journal_error')]:
            with self.subTest(target=target):
                self.state.unlink(missing_ok=True)
                observer = HookRecorder()
                original = ValueError('SECRET render/state/journal')
                with patch.object(poller, target, side_effect=original):
                    with self.assertRaises(ValueError) as caught:
                        poller.deliver(StubTransport(), self.state, self.helper, observer=observer)
                self.assertIs(caught.exception, original)
                self.assertTrue(observer.dirty)
                self.assertIn(stage, [f.get('condition') for f in observer.frames])
                self.assertEqual(observer.frames[-1]['phase'], 'terminal')

    def test_helper_timeout_and_control_never_replay_or_journal(self):
        for original in [subprocess.TimeoutExpired(['SECRET_ARGV'], 120), KeyboardInterrupt('CONTROL')]:
            with self.subTest(kind=type(original).__name__):
                observer = HookRecorder()
                with patch.object(poller.subprocess, 'run', side_effect=original) as run:
                    with self.assertRaises(type(original)) as caught:
                        poller.deliver(StubTransport(), self.state, self.helper, observer=observer)
                self.assertIs(caught.exception, original)
                self.assertEqual(run.call_count, 1)
                self.assertFalse(self.state.exists())
                self.assertTrue(observer.dirty)

    def test_original_exception_survives_cleanup_observer_interrupt(self):
        class Interrupted(HookRecorder):
            def emit(self, phase, **fields):
                if phase != 'begin':
                    raise SystemExit('OBSERVER_CONTROL')
                return super().emit(phase, **fields)
        original = KeyboardInterrupt('ORIGINAL')
        with patch.object(poller, 'load_state', side_effect=original):
            with self.assertRaises(KeyboardInterrupt) as caught:
                poller.deliver(StubTransport(), self.state, self.helper, observer=Interrupted())
        self.assertIs(caught.exception, original)

    def test_sink_requested_is_not_smtp_success_and_nonzero_is_dirty(self):
        observer = HookRecorder()
        poller.os.environ['ALERT_LOG'] = 'SECRET_LOG_PATH'
        self.helper_returncode = 75
        # Override fake helper to avoid treating arbitrary test path as a file.
        with patch.object(poller.subprocess, 'run', return_value=types.SimpleNamespace(returncode=75)):
            self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper,
                                          observer=observer), {'sent': 0, 'errors': 1})
        self.assertTrue(observer.dirty)
        self.assertTrue(all(f['flags']['log_requested'] for f in observer.frames))
        self.assertEqual(sum(f.get('decision') == 'alert' for f in observer.frames), 1)
        self.assertFalse(self.state.exists())
        self.assertNotIn('SECRET', json.dumps(observer.frames))

    def selected_client(self):
        spec = importlib.util.spec_from_file_location('synthetic_selected_client', REPO/'scripts/hub_alert_client.py')
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        return client

    def test_frame_capacity_negative_ack_and_later_frames_never_heal(self):
        client = self.selected_client()
        frames = []
        def exchange(path, request, **kwargs):
            frame = request['frame']
            frames.append(frame)
            return {'seq': frame['seq'], 'ok': False}
        args = types.SimpleNamespace(alert_client=Path('/synthetic/client'), alert_client_sha256='a'*64,
                                     alert_broker_socket='/synthetic/socket')
        with patch.object(poller, 'alert_client', return_value=client), patch.object(client, 'exchange', exchange):
            observer = poller.AlertObserver(args)
            items = [(str(i), {}, {}) for i in range(30)]
            with patch.object(poller, 'pending_alerts', return_value=iter(items)):
                result = poller.deliver(StubTransport(), self.state, self.helper, observer=observer)
        self.assertEqual(result, {'sent': 30, 'errors': 0})
        self.assertEqual(len(self.helper_calls), 30)
        self.assertEqual(len(frames), 16)
        self.assertEqual([f['seq'] for f in frames], list(range(16)))
        self.assertTrue(observer.dirty)
        self.assertTrue(frames[0]['observer_ok'])
        self.assertTrue(all(not f['observer_ok'] for f in frames[1:]))
        self.assertNotIn('terminal', [f['phase'] for f in frames], 'overflow cannot become clean completion')

    def test_client_source_pin_and_context_refusals(self):
        client = self.selected_client()
        storage = REPO / '.local/tmp'
        storage.mkdir(mode=0o700, parents=True, exist_ok=True)
        trusted = tempfile.TemporaryDirectory(dir=storage, prefix='trusted-alert-client-')
        self.addCleanup(trusted.cleanup)
        path = Path(trusted.name) / 'client.py'
        raw = (REPO/'scripts/hub_alert_client.py').read_bytes()
        path.write_bytes(raw)
        path.chmod(0o600)
        import hashlib
        loaded = poller.alert_client(path, hashlib.sha256(raw).hexdigest())
        self.assertTrue(callable(loaded.positive_only_context))
        with self.assertRaises(ValueError):
            poller.alert_client(path, '0'*64)
        for role, parent, env in [('unknown',None,{}), ('intake_mailer','a'*32,{}),
                                  ('intake_mailer',None,{'HUB_ALERT_ROLE':'quota_monitor'})]:
            with self.assertRaises(client.Refused):
                client.positive_only_context(role, socket_path='/synthetic/socket', parent_id=parent, env=env)
        self.assertEqual(client.positive_only_context('intake_mailer',socket_path='/synthetic/socket',env={}).role,
                         'intake_mailer')

    def test_client_source_pin_refuses_world_writable_ancestor(self):
        import hashlib
        storage = REPO / '.local/tmp'
        storage.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=storage, prefix='untrusted-alert-client-') as owned:
            untrusted = Path(owned) / 'world-writable'
            untrusted.mkdir(mode=0o777)
            untrusted.chmod(0o777)
            path = untrusted / 'client.py'
            raw = (REPO/'scripts/hub_alert_client.py').read_bytes()
            path.write_bytes(raw)
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'observer_unavailable'):
                poller.alert_client(path, hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.helper_calls, [])

    def test_closed_broker_contract_retains_unqualified_claim_without_admission(self):
        # Release-tooling CI does not install a Hub2 host package. Load the
        # actual checkout package under a unique name, never a legacy fallback.
        name = '_intake_mailer_real_host'
        source = REPO/'hub2/host/agent_hub_host'
        spec = importlib.util.spec_from_file_location(name, source/'__init__.py',
                                                      submodule_search_locations=[str(source)])
        package = importlib.util.module_from_spec(spec)
        previous = {key:value for key,value in sys.modules.items() if key == name or key.startswith(name+'.')}
        self.addCleanup(lambda: sys.modules.update(previous))
        self.addCleanup(lambda: [sys.modules.pop(key,None) for key in list(sys.modules)
                                 if key == name or key.startswith(name+'.')])
        sys.modules[name] = package
        spec.loader.exec_module(package)
        authority = importlib.import_module(name+'.alert_authority')
        broker = importlib.import_module(name+'.alert_broker')
        client = self.selected_client()
        frame = dict(v=1, invocation_id='a'*32,parent_id=None,role='intake_mailer',phase='begin',seq=0,
                     decision=None,condition=None,flags={'log_requested':True},recipient_class='none',observer_ok=True)
        self.assertEqual(client.checked(frame), broker.checked(frame))
        process = authority.ProcessKey(123,456,'11111111-1111-1111-1111-111111111111')
        pin = authority.SourcePin(1,2,'a'*64)
        ram = authority.LiveRAM({role:pin for role in authority.ROLES}, broker.checked)
        instance = broker.Broker.__new__(broker.Broker)
        instance.identity=lambda _:process
        instance.ram=ram;instance.dispatch=None
        instance.unqualified_frames=[];instance.unqualified_contexts={};instance.unqualified_bytes=0
        with patch.object(broker.os,'getuid',return_value=1000):
            answer = instance.request({'v':1,'op':'hook','frame':frame},(123,1000,1000))
        self.assertIs(answer['ok'],False)
        self.assertEqual(len(instance.unqualified_frames),1)
        self.assertEqual(ram.active,{})
        self.assertNotIn('intake_mailer',authority.ROLES)
        self.assertNotIn('intake_mailer',ram.counts)
        partial=authority.LiveRAM._selected_quota_attempt(pin,broker.checked)
        self.assertEqual(set(partial.pins),{'quota_monitor'})
        self.assertNotIn('intake_mailer',partial.pins)
        for dirty in [dict(frame,secret='PRIVATE'),dict(frame,phase='decision',decision='alert',condition='PRIVATE'),
                      dict(frame,parent_id='b'*32),
                      dict(frame,phase='decision',decision='alert',condition='mailer_api_error'),
                      dict(frame,phase='decision',decision='alert',condition='intake_recovery')]:
            with self.assertRaises(broker.Refused):broker.checked(dirty)



class LoaderDescriptorCleanupTest(unittest.TestCase):
    """Pure descriptor-number fakes; no host FD/path/config is opened."""
    def load(self, *, close_errors=None, read_error=None, source_open_error=None):
        import contextlib
        import hashlib
        import stat
        raw = b'SYNTHETIC_CLIENT = True\n'
        directory = types.SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o700)
        source = types.SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o600, st_nlink=1,
            st_size=len(raw),st_dev=1,st_ino=2,st_mtime_ns=3,st_ctime_ns=4)
        acquired, closed = [], []
        errors = close_errors or {}
        def opened(name, flags, **kwargs):
            if name == 'client.py' and source_open_error is not None:
                raise source_open_error
            fd = 100 + len(acquired)
            acquired.append(fd)
            return fd
        def close(fd):
            closed.append(fd)
            if closed.count(fd) > 1:
                raise AssertionError('a possibly consumed descriptor was retried')
            if fd in errors:
                raise errors[fd]
        def read(fd, limit):
            if read_error is not None:
                raise read_error
            return raw
        with contextlib.ExitStack() as stack:
            for name, replacement in [('open',opened),('close',close),('read',read),
                ('getuid',lambda:1000),('fstat',lambda fd:source if fd==102 else directory),
                ('stat',lambda *args,**kwargs:source)]:
                stack.enter_context(patch.object(poller.os,name,replacement))
            try:
                result = poller.alert_client(Path('/owned/client.py'), hashlib.sha256(raw).hexdigest())
                error = None
            except BaseException as caught:
                result, error = None, caught
        return acquired, closed, result, error

    def test_old_directory_close_error_consumes_once_and_closes_new_fd(self):
        original = OSError('synthetic consumed old descriptor')
        acquired,closed,result,error = self.load(close_errors={100:original})
        self.assertEqual(acquired,[100,101])
        self.assertEqual(closed,[100,101])
        self.assertIs(error,original)
        self.assertIsNone(result)

    def test_old_directory_close_control_is_preserved_and_new_fd_closed(self):
        original = KeyboardInterrupt('original cleanup control')
        _,closed,_,error = self.load(close_errors={100:original})
        self.assertEqual(closed,[100,101])
        self.assertIs(error,original)

    def test_source_close_error_does_not_skip_directory_or_execute_module(self):
        original = OSError('source close failed')
        _,closed,result,error = self.load(close_errors={102:original})
        self.assertEqual(closed,[100,102,101])
        self.assertIs(error,original)
        self.assertIsNone(result)

    def test_source_close_control_does_not_skip_directory(self):
        original = SystemExit('source close control')
        _,closed,_,error = self.load(close_errors={102:original,101:OSError('later close')})
        self.assertEqual(closed,[100,102,101])
        self.assertIs(error,original)

    def test_original_read_exception_wins_over_all_cleanup_controls(self):
        for original in [ValueError('original read error'),KeyboardInterrupt('original read control')]:
            with self.subTest(kind=type(original).__name__):
                _,closed,_,error=self.load(read_error=original,
                    close_errors={102:SystemExit('cleanup control'),101:OSError('cleanup error')})
                self.assertEqual(closed,[100,102,101])
                self.assertIs(error,original)

    def test_source_open_failure_closes_every_acquired_fd_once(self):
        original=OSError('source open failed')
        acquired,closed,_,error=self.load(source_open_error=original)
        self.assertEqual(acquired,[100,101])
        self.assertEqual(closed,[100,101])
        self.assertIs(error,original)

    def test_normal_loader_closes_all_once_before_executing_selected_bytes(self):
        acquired,closed,result,error=self.load()
        self.assertEqual(acquired,[100,101,102])
        self.assertEqual(closed,[100,102,101])
        self.assertIsNone(error)
        self.assertTrue(result.SYNTHETIC_CLIENT)


class HistoryPagingTest(RecoveryAlertTest):
    def test_mails_alert_older_than_50_context_events_once(self):
        class OldAlertTransport(StubTransport):
            def get(self, path):
                if path == '/api/intakes/abc-1/context':
                    self.paths.append(path)
                    return {'task': {'owner': self.owner}, 'events': [
                        {'seq': i, 'kind': 'worker_append', 'body': {}} for i in range(51, 101)]}
                if path == '/api/intakes/abc-1/history?after=0&limit=100':
                    self.paths.append(path)
                    return {'events': [
                        {'seq': 10, 'kind': 'developer_alert_raised', 'body': ALERT},
                        {'seq': 51, 'kind': 'worker_append', 'body': {}}], 'next': None}
                return super().get(path)

        observer = HookRecorder()
        self.assertEqual(poller.deliver(OldAlertTransport(), self.state, self.helper, observer=observer),
                         {'sent': 1, 'errors': 0})
        self.assertFalse(observer.dirty)
        self.assertEqual(json.loads(self.state.read_text())['sent'], ['abc-1:10'])
        self.assertEqual(poller.deliver(OldAlertTransport(), self.state, self.helper), {'sent': 0, 'errors': 0})
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_multipages_history_and_marks_complete(self):
        class MultiPageTransport(StubTransport):
            def get(self, path):
                if path == '/api/intakes/abc-1/history?after=0&limit=100':
                    self.paths.append(path)
                    return {'events': [
                        {'seq': i, 'kind': 'worker_append', 'body': {}} for i in range(1, 101)], 'next': 100}
                if path == '/api/intakes/abc-1/history?after=100&limit=100':
                    self.paths.append(path)
                    return {'events': [
                        {'seq': 150, 'kind': 'developer_alert_raised', 'body': ALERT}], 'next': None}
                return super().get(path)

        observer = HookRecorder()
        transport = MultiPageTransport()
        self.assertEqual(poller.deliver(transport, self.state, self.helper, observer=observer),
                         {'sent': 1, 'errors': 0})
        self.assertFalse(observer.dirty)
        self.assertIn('/api/intakes/abc-1/history?after=0&limit=100', transport.paths)
        self.assertIn('/api/intakes/abc-1/history?after=100&limit=100', transport.paths)
        self.assertIn('mailer_scan_complete', [f.get('condition') for f in observer.frames])
        self.assertEqual(json.loads(self.state.read_text())['sent'], ['abc-1:150'])

    def test_malformed_nonprogress_and_page_cap_stay_dirty_and_unsuccessful(self):
        bad_pages = [
            ('missing_next', {'events': [{'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}]}),
            ('bool_next', {'events': [{'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}], 'next': True}),
            ('str_next', {'events': [{'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}], 'next': '9'}),
            ('wrong_next', {'events': [{'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}], 'next': 5}),
            ('empty_with_next', {'events': [], 'next': 9}),
            ('non_list_events', {'events': None, 'next': None}),
            ('non_increasing_seq', {'events': [
                {'seq': 9, 'kind': 'worker_append', 'body': {}},
                {'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}], 'next': None}),
            ('seq_below_after', {'events': [{'seq': 0, 'kind': 'developer_alert_raised', 'body': ALERT}], 'next': None}),
        ]
        for label, payload in bad_pages:
            with self.subTest(case=label):
                class BadHistoryTransport(StubTransport):
                    def get(self, path):
                        if path.startswith('/api/intakes/abc-1/history?'):
                            self.paths.append(path)
                            return payload
                        return super().get(path)

                observer = HookRecorder()
                result = poller.deliver(BadHistoryTransport(), self.state, self.helper, observer=observer)
                self.assertEqual(result['errors'], 1)
                self.assertEqual(result['sent'], 0)
                self.assertTrue(observer.dirty)
                conditions = [f.get('condition') for f in observer.frames]
                self.assertIn('mailer_scan_incomplete', conditions)
                self.assertNotIn('mailer_scan_complete', conditions)
                self.assertNotIn('mailer_scan_empty', conditions)

        class CapTransport(StubTransport):
            def get(self, path):
                if path == '/api/intakes/abc-1/history?after=0&limit=100':
                    self.paths.append(path)
                    return {'events': [{'seq': 1, 'kind': 'worker_append', 'body': {}}], 'next': 1}
                if path == '/api/intakes/abc-1/history?after=1&limit=100':
                    self.paths.append(path)
                    return {'events': [{'seq': 2, 'kind': 'worker_append', 'body': {}}], 'next': 2}
                return super().get(path)

        observer = HookRecorder()
        with patch.object(poller, 'MAX_HISTORY_PAGES', 2):
            result = poller.deliver(CapTransport(), self.state, self.helper, observer=observer)
        self.assertEqual(result, {'sent': 0, 'errors': 1})
        self.assertTrue(observer.dirty)
        self.assertIn('mailer_scan_incomplete', [f.get('condition') for f in observer.frames])

    def test_earlier_helper_failure_then_later_success_retries_without_watermark(self):
        class TwoAlertsTransport(StubTransport):
            def get(self, path):
                if path == '/api/intakes/abc-1/history?after=0&limit=100':
                    self.paths.append(path)
                    return {'events': [
                        {'seq': 10, 'kind': 'developer_alert_raised', 'body': ALERT},
                        {'seq': 12, 'kind': 'developer_alert_raised', 'body': dict(ALERT, session_id='s-4')}], 'next': None}
                return super().get(path)

        calls = []

        def flaky_helper(argv, *, env, **kwargs):
            calls.append(env['PING_ALERT_MESSAGE'])
            if len(calls) == 1:
                return types.SimpleNamespace(returncode=3)
            with self.log.open('a') as stream:
                stream.write(env['PRINCIPAL_ALERT_TO'] + '|' + env['PING_ALERT_MESSAGE'] + '\n')
            return types.SimpleNamespace(returncode=0)

        observer = HookRecorder()
        with patch.object(poller.subprocess, 'run', side_effect=flaky_helper):
            first = poller.deliver(TwoAlertsTransport(), self.state, self.helper, observer=observer)
        self.assertEqual(first, {'sent': 1, 'errors': 1})
        self.assertTrue(observer.dirty)
        self.assertEqual(json.loads(self.state.read_text())['sent'], ['abc-1:12'])

        observer2 = HookRecorder()
        second = poller.deliver(TwoAlertsTransport(), self.state, self.helper, observer=observer2)
        self.assertEqual(second, {'sent': 1, 'errors': 0})
        self.assertFalse(observer2.dirty)
        self.assertEqual(json.loads(self.state.read_text())['sent'], ['abc-1:12', 'abc-1:10'])
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_history_api_error_marks_dirty_and_preserves_host_alerts(self):
        class HistoryErrorTransport(StubTransport):
            def get(self, path):
                if path.startswith('/api/intakes/abc-1/history?'):
                    self.paths.append(path)
                    raise OSError('history unavailable')
                return super().get(path)

        observer = HookRecorder()
        result = poller.deliver(HistoryErrorTransport(machines=[HOST]), self.state, self.helper, observer=observer)
        self.assertEqual(result, {'sent': 1, 'errors': 1})
        self.assertTrue(observer.dirty)
        conditions = [f.get('condition') for f in observer.frames]
        self.assertIn('mailer_api_error', conditions)
        self.assertIn('mailer_scan_incomplete', conditions)


class EngineUnhealthyAlertTest(RecoveryAlertTest):
    def test_mails_developer_once_per_engine_preflight_failure(self):
        failed = {'engine': 'go', 'reason': 'engine_auth_check_unavailable', 'session_id': '62867a7a-095d',
                  'failed_at': 1_800_000_000_000, 'until': 1_800_001_800_000}
        machines = [{'machine_id': 'hetzner-aplexer', 'connected': True, 'unhealthy_engines': [failed]},
                    {'machine_id': 'quiet', 'connected': True}]
        transport = StubTransport(owner='human:x', machines=machines)
        self.assertEqual(poller.deliver(transport, self.state, self.helper), {'sent': 1, 'errors': 0})
        to, message = self.log.read_text().splitlines()[0].split('|', 1)
        self.assertEqual(to, 'dev@dtcdev.click')
        self.assertIn('Hub2 engine go failed its auth/preflight check on hetzner-aplexer', message)
        self.assertIn('engine_auth_check_unavailable', message)
        self.assertIn('repicks another engine', message)
        self.assertEqual(poller.deliver(transport, self.state, self.helper), {'sent': 0, 'errors': 0})
        again = dict(machines[0], unhealthy_engines=[dict(failed, failed_at=1_800_000_600_000)])
        self.assertEqual(poller.deliver(StubTransport(owner='human:x', machines=[again]), self.state, self.helper),
                         {'sent': 1, 'errors': 0}, 'a new failure alerts again')

    def test_malformed_engine_entries_are_ignored(self):
        machines = [{'machine_id': 'h1', 'unhealthy_engines': [{'engine': 'Go Bad', 'failed_at': 1},
                     {'engine': 'go', 'failed_at': '1'}, 'x']}]
        self.assertEqual(poller.deliver(StubTransport(owner='human:x', machines=machines), self.state, self.helper),
                         {'sent': 0, 'errors': 0})


if __name__ == '__main__':
    unittest.main()
