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
    def __init__(self, owner=poller.RECIPIENT):
        self.owner = owner
        self.paths = []

    def get(self, path):
        self.paths.append(path)
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

    def test_skips_intakes_someone_already_owns(self):
        self.assertEqual(poller.deliver(StubTransport(owner='human:x'), self.state, self.helper), {'sent': 0, 'errors': 0})
        self.assertFalse(self.log.exists())

    def test_failed_helper_is_retried_next_run(self):
        self.helper.write_text('#!/bin/sh\nexit 3\n')
        self.assertEqual(poller.deliver(StubTransport(), self.state, self.helper), {'sent': 0, 'errors': 1})
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
