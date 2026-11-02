import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from agent_hub_host import auth, human_auth, device_transport
from agent_hub_host import _vendored_device_login as flow


class DeviceTransportTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.sleeps = []

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def poster(self):
        return device_transport.poster(clock=lambda: self.now, sleeper=self.sleep)

    def error(self, status, payload):
        return urllib.error.HTTPError('https://hub.test/api/device/token', status, '', {}, io.BytesIO(payload))

    def test_mid_poll_html_recovers_in_shared_flow(self):
        for status, payload in [(502, b'<html>CDN</html>'), (503, b'{}'), (599, b'{}')]:
            with self.subTest(status=status):
                self.setUp()
                responses = [
                    {'device_code': 'device', 'user_code': 'user', 'expires_in': 30, 'interval': 1},
                    self.error(400, b'{"error":"authorization_pending"}'),
                    self.error(status, payload),
                    RuntimeError('Expected JSON response'),
                    json.JSONDecodeError('invalid JSON', '', 0),
                    {'access_token': 'test-token'},
                ]
                with patch.object(device_transport.api, 'safe_http_json', side_effect=responses):
                    result = flow.device_login('https://hub.test', self.poster(),
                        sleeper=self.sleep, clock=lambda: self.now, prompt=lambda _: None)
                self.assertEqual(result, {'access_token': 'test-token'})
                self.assertEqual(self.sleeps, [1, 1, 1, 2, 4])

    def test_retries_stop_at_expiry(self):
        post = self.poster()
        with patch.object(device_transport.api, 'safe_http_json', return_value={
            'device_code': 'd', 'expires_in': 3,
        }):
            post('https://hub.test/code', {})
        with patch.object(device_transport.api, 'safe_http_json', side_effect=RuntimeError('Expected JSON response')) as request:
            status, result = post('https://hub.test/token', {'grant_type': flow.GRANT_TYPE})
        self.assertEqual((status, result), (400, {'error': 'expired_token'}))
        self.assertEqual(self.now, 3)
        self.assertEqual(request.call_count, 2)
        self.assertEqual([c.kwargs['timeout'] for c in request.call_args_list], [3, 2])

    def test_bounded_attempts_and_exponential_backoff(self):
        with patch.object(device_transport.api, 'safe_http_json', side_effect=urllib.error.URLError('offline')) as request:
            with self.assertRaises(urllib.error.URLError):
                self.poster()('https://hub.test/code', {})
        self.assertEqual(request.call_count, 6)
        self.assertEqual(self.sleeps, [1, 2, 4, 8, 16])

    def test_oauth_errors_are_not_retried(self):
        for code in ['authorization_pending', 'slow_down', 'access_denied', 'expired_token']:
            with self.subTest(code=code), patch.object(device_transport.api, 'safe_http_json',
                side_effect=self.error(400, json.dumps({'error': code}).encode())) as request:
                self.assertEqual(self.poster()('https://hub.test/token', {}), (400, {'error': code}))
                self.assertEqual(request.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_security_failures_are_not_retried(self):
        for error in [RuntimeError('Response too large'), ValueError('Token exchange response mismatch')]:
            with patch.object(device_transport.api, 'safe_http_json', side_effect=error) as request:
                with self.assertRaises(type(error)):
                    self.poster()('https://hub.test/token', {})
                self.assertEqual(request.call_count, 1)

    def test_host_and_human_share_transport_and_exchange(self):
        exchange = object()
        for call in [lambda: auth._post_device('https://hub.test/token', {}, exchange),
                     lambda: human_auth._poster(exchange)('https://hub.test/token', {})]:
            with patch.object(device_transport, 'poster') as factory:
                call()
                factory.assert_called_once_with(exchange)
                factory.return_value.assert_called_once_with('https://hub.test/token', {})
