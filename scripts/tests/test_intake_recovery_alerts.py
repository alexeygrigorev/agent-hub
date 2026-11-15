import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

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
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 0, 'errors': 1})
        self.assertFalse(self.state.exists())


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


if __name__ == '__main__':
    unittest.main()
