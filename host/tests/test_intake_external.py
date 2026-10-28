"""The supported append CLI sends external registration to the shared API."""
import io
import json
import unittest
from unittest.mock import patch
from agent_hub_host import cli, intake


class ExternalRegistrationCLI(unittest.TestCase):
    def test_verified_session_append(self):
        event = {'kind': 'session', 'native_id': '3261084c-1f4f-4d9d-b88c-1eb74aa1810a',
                 'host': 'fixture', 'verify_live': True}
        with patch('sys.argv', ['hub', 'intake', 'append', 'fixture-task', '--version', '7',
                                '--key', 'register-external', '--event-file', '-']), \
             patch.object(intake, 'read_file', return_value=json.dumps(event)), \
             patch.object(intake, 'request', return_value={'ok': True}) as request, \
             patch('sys.stdout', io.StringIO()):
            cli.main()
        request.assert_called_once_with('POST', '/api/intake/tasks/fixture-task/events',
                                        {'key': 'register-external', 'expected_version': 7, 'event': event})
