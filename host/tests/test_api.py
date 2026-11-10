import io
import urllib.request
import urllib.error
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
from unittest.mock import patch, MagicMock
from agent_hub_host import api

class MockClock:
    def __init__(self):
        self._time = 0.0
    def time(self):
        return self._time
    def sleep(self, s):
        self._time += s
        
class MockRNG:
    def random(self):
        return 0.5
    def randbytes(self, n):
        return b"x" * n

class TestApi(unittest.TestCase):
    def test_release_bootstrap_503_logs_actionable_hub2_url(self):
        url = api.config.LEGACY_WORKERS_BASE_URL + "/api/sessions"
        body = b'{"error":"release_bootstrap","dispatch_enabled":false}'
        error = urllib.error.HTTPError(
            url, 503, "Service unavailable", {"Content-Length": str(len(body))}, io.BytesIO(body)
        )
        opener = MagicMock()
        opener.open.side_effect = error
        request = urllib.request.Request(url, headers={"Authorization": "Bearer synthetic-secret"})
        logged = io.StringIO()

        with patch.object(api, "_get_opener", return_value=opener), \
             patch("agent_hub_host._prod_guard.assert_request_allowed"), \
             patch("sys.stderr", logged):
            with self.assertRaises(api.LegacyHubEndpointError) as ctx:
                api.safe_http_json(request)

        message = str(ctx.exception)
        self.assertIn("HTTP 503 release_bootstrap", message)
        self.assertIn("https://hub.dtcdev.click", message)
        self.assertIn("hub auth login --url https://hub.dtcdev.click", message)
        self.assertIn('"event": "hub_endpoint_misconfigured"', logged.getvalue())
        self.assertIn('"replacement_url": "https://hub.dtcdev.click"', logged.getvalue())
        self.assertNotIn("synthetic-secret", logged.getvalue())

    def test_non_bootstrap_503_preserves_its_error_body(self):
        url = "https://hub.example.test/api/sessions"
        body = b'{"error":"temporarily_unavailable"}'
        error = urllib.error.HTTPError(
            url, 503, "Service unavailable", {"Content-Length": str(len(body))}, io.BytesIO(body)
        )
        opener = MagicMock()
        opener.open.side_effect = error
        request = urllib.request.Request(url)

        with patch.object(api, "_get_opener", return_value=opener), \
             patch("agent_hub_host._prod_guard.assert_request_allowed"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                api.safe_http_json(request)

        self.assertEqual(ctx.exception.read(), body)

    @patch("agent_hub_host.gate.get_gate_status", return_value={"launch_allowed": True})
    @patch("agent_hub_host.cpu_resources.sample_cpu_usage", return_value={"observed_at": "2026-11-03T00:00:00Z"})
    @patch("agent_hub_host.host_memory.read_host_swap_used_percent", return_value=81.5)
    @patch("agent_hub_host.host_memory.read_host_memory", return_value={"available_bytes": 1024, "total_bytes": 2048})
    def test_heartbeat_reports_swap_pressure_sample(self, _memory, _swap, _cpu, _gate):
        payload = api.get_heartbeat_payload()
        self.assertEqual(payload["swap_used_percent"], 81.5)

    @patch("urllib.request.OpenerDirector.open")
    @patch("agent_hub_host.auth.get_host_access_token")
    def test_register_machine(self, mock_get_token, mock_urlopen):
        mock_get_token.return_value = "acc123"
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json"}
        resp.read.return_value = b"{\"status\": \"ok\"}"
        mock_urlopen.return_value.__enter__.return_value = resp
        
        res = api.register_machine("https://fake")
        self.assertIsNone(res)
        
    def test_ws_frame_encoding(self):
        class MockSock:
            def __init__(self):
                self.sent = b""
            def sendall(self, data):
                self.sent += data
        
        sock = MockSock()
        rng = MockRNG()
        from agent_hub_host.ws import WSConnection
        ws = WSConnection(sock, b"", rng)
        ws.send_frame(1, b"hello")
        self.assertTrue(len(sock.sent) > 0)
        
    def test_ws_frame_decoding(self):
        class MockSock:
            def recv(self, n):
                return b""
        
        payload = b"hello"
        header = bytearray([0x81, len(payload)])
        rest = bytes(header) + payload
        
        from agent_hub_host.ws import WSConnection
        ws = WSConnection(MockSock(), rest, MockRNG())
        fin, opcode, decoded = ws.recv_frame()
        self.assertTrue(fin)
        self.assertEqual(opcode, 1)
        self.assertEqual(decoded, b"hello")

    @patch("urllib.request.OpenerDirector.open")
    @patch("agent_hub_host.auth.get_host_access_token")
    def test_bearer_not_forwarded(self, mock_get_token, mock_urlopen):
        mock_get_token.return_value = "acc123"
        # We need to test if the custom NoRedirectHandler raises HTTPError rather than forwarding Bearer token
        from agent_hub_host.api import NoRedirectHandler
        handler = NoRedirectHandler()
        req = urllib.request.Request("https://fake")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", None, "https://other")

    @patch("urllib.request.OpenerDirector.open")
    @patch("agent_hub_host.auth.get_host_access_token")
    def test_no_token_in_exception(self, mock_get_token, mock_urlopen):
        mock_get_token.return_value = "secret_token_123"
        mock_urlopen.side_effect = Exception("Some connection error")
        
        try:
            api.register_machine("https://fake")
        except Exception as e:
            self.assertNotIn("secret_token_123", str(e))


class TestUserAgent(unittest.TestCase):
    def test_requests_identify_the_client_instead_of_python_urllib(self):
        from agent_hub_host import api
        opener = api._get_opener()
        self.assertIn(('User-Agent', api.USER_AGENT), opener.addheaders)
        self.assertNotIn('Python-urllib', api.USER_AGENT)


class TestGetAvailableMachinesAuth(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError('https://hub.invalid/api/machines/available', code, 'err', {}, None)

    @patch("agent_hub_host.api.safe_http_json")
    @patch("agent_hub_host.auth.refresh_token")
    @patch("agent_hub_host.auth.get_host_access_token")
    @patch("agent_hub_host.api._host_cfg")
    def test_401_without_refresh_token_surfaces_401(self, mock_cfg, mock_token, mock_refresh, mock_http):
        mock_cfg.return_value = {"base_url": "https://hub.invalid"}
        mock_token.return_value = "admin-bearer-without-host-scope"
        mock_http.side_effect = self._http_error(401)
        with self.assertRaises(RuntimeError) as ctx:
            api.get_available_machines("https://hub.invalid")
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("login", str(ctx.exception).lower())
        mock_refresh.assert_not_called()

    @patch("agent_hub_host.api.safe_http_json")
    @patch("agent_hub_host.auth.refresh_token")
    @patch("agent_hub_host.auth.get_host_access_token")
    @patch("agent_hub_host.api._host_cfg")
    def test_401_with_refresh_token_retries(self, mock_cfg, mock_token, mock_refresh, mock_http):
        mock_cfg.return_value = {"base_url": "https://hub.invalid", "refresh_token": "r123"}
        mock_token.side_effect = ["expired-token", "fresh-token"]
        mock_http.side_effect = [self._http_error(401), {"machines": []}]
        res = api.get_available_machines("https://hub.invalid")
        self.assertEqual(res, {"machines": []})
        mock_refresh.assert_called_once_with()

    @patch("agent_hub_host.api.safe_http_json")
    @patch("agent_hub_host.auth.refresh_token")
    @patch("agent_hub_host.auth.get_host_access_token")
    @patch("agent_hub_host.api._host_cfg")
    def test_non_401_never_refreshes(self, mock_cfg, mock_token, mock_refresh, mock_http):
        mock_cfg.return_value = {"base_url": "https://hub.invalid", "refresh_token": "r123"}
        mock_token.return_value = "tok"
        mock_http.side_effect = self._http_error(500)
        with self.assertRaises(RuntimeError) as ctx:
            api.get_available_machines("https://hub.invalid")
        self.assertIn("500", str(ctx.exception))
        mock_refresh.assert_not_called()


def test_hub_opener_ignores_environment_proxy(monkeypatch):
    import urllib.request
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    from agent_hub_host import api
    handlers = [h for h in api._get_opener().handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert all(not h.proxies for h in handlers)  # empty ProxyHandler registers no *_open methods


def test_hub_unreachable_guard_logs_once_with_suspect(monkeypatch):
    import socket
    import urllib.error
    from agent_hub_host import api
    api._note_hub_success()
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:8899')
    lines = []
    err = urllib.error.URLError(ConnectionRefusedError(10061, 'refused'))
    assert not api._note_hub_failure(err, now=1000.0, emit=lines.append)
    assert not api._note_hub_failure(err, now=1100.0, emit=lines.append)
    assert api._note_hub_failure(err, now=1121.0, emit=lines.append)
    assert not api._note_hub_failure(err, now=1200.0, emit=lines.append)
    assert len(lines) == 1 and 'HTTPS_PROXY' in lines[0] and 'refused' in lines[0] and '8899' not in lines[0]
    api._note_hub_success()
    assert api._hub_outage['since'] is None
    dns = urllib.error.URLError(socket.gaierror(11001, 'no host'))
    assert 'DNS' in api.hub_unreachable_suspect(dns)


def test_package_import_adds_hub_to_no_proxy():
    from agent_hub_host import ensure_hub_no_proxy
    env = {'NO_PROXY': 'localhost'}
    ensure_hub_no_proxy(env)
    ensure_hub_no_proxy(env)
    assert env['NO_PROXY'] == 'localhost,hub.dtcdev.click,.dtcdev.click'
    assert env['no_proxy'] == 'hub.dtcdev.click,.dtcdev.click'
