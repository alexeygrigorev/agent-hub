import io
import json
import unittest
from urllib.error import HTTPError

from agent_hub_host.session_control import http_error


def _err(status, body):
    return HTTPError('https://hub.invalid/api/sessions/x', status, 'x', {}, io.BytesIO(json.dumps(body).encode()))


class UnknownSessionCauseTest(unittest.TestCase):
    def test_route_not_found_names_cause(self):
        msg = str(http_error(_err(404, {'error': 'route_not_found'})))
        self.assertIn('(route_not_found)', msg)
        self.assertIn('not Hub-managed', msg)
        self.assertIn('aplexer show', msg)

    def test_session_not_found_names_scope_cause(self):
        msg = str(http_error(_err(404, {'error': 'session_not_found'})))
        self.assertIn("outside this credential's intake scope", msg)

    def test_other_404_unchanged(self):
        msg = str(http_error(_err(404, {'error': 'intake_not_found'})))
        self.assertNotIn('not Hub-managed', msg)


if __name__ == '__main__':
    unittest.main()
