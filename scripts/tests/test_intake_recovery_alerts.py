import importlib.util
import json
from pathlib import Path
import tempfile
import types
import shutil
import subprocess
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
        assert path == '/api/intakes/abc-1/context'
        return {'task': {'owner': self.owner}, 'events': [
            {'seq': 7, 'kind': 'worker_append', 'body': {}},
            {'seq': 9, 'kind': 'developer_alert_raised', 'body': ALERT}]}


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


if __name__ == '__main__':
    unittest.main()
