import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
from unittest.mock import patch, MagicMock
from agent_hub_host import api, config

class MockClock:
    def __init__(self):
        self._time = 0.0
    def time(self):
        return self._time
    def monotonic(self):
        return self._time
    def sleep(self, s):
        self._time += s

class MockRNG:
    def random(self): return 0.5
    def randbytes(self, n): return b"x" * n

class InertQuotaCollector:
    def __init__(self, *args, **kwargs):
        self.base_url = None
    def step(self):
        pass

class TestAgent(unittest.TestCase):
    def setUp(self):
        self._orig_config = getattr(config, "_CONFIG", None)
        config._CONFIG = {"base_url": "https://test", "access_token": "acc123"}
        self._host_cfg_patcher = patch(
            "agent_hub_host.api._host_cfg",
            return_value={"base_url": "https://test", "access_token": "acc123"},
        )
        self._host_cfg_patcher.start()
        self.clock = MockClock()
        self.rng = MockRNG()
        self.quota_collector = InertQuotaCollector()

    def tearDown(self):
        self._host_cfg_patcher.stop()
        config._CONFIG = self._orig_config

    @patch("agent_hub_host.auth.get_host_access_token")
    @patch("agent_hub_host.auth.get_access_token")
    @patch("agent_hub_host.api.register_machine")
    @patch("agent_hub_host.api.ws_handshake")
    @patch("agent_hub_host.api.HostAgent._poll_fallback")
    def test_agent_immediate_close_backoff(self, mock_poll, mock_ws_handshake, mock_reg, mock_get_token, mock_get_host_token):
        mock_get_token.return_value = "acc123"
        mock_get_host_token.return_value = "acc123"
        agent = api.HostAgent(clock=self.clock, rng=self.rng, quota_collector=self.quota_collector)
        
        iteration = 0
        def ws_side_effect(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            if iteration >= 5:
                agent.shutdown_requested = True
            raise ConnectionError("WS Failed")
            
        mock_ws_handshake.side_effect = ws_side_effect
        agent.run()
        
        self.assertEqual(mock_ws_handshake.call_count, 5)
        self.assertEqual(mock_poll.call_count, 5)
        # 1 + 2 + 4 + 8 = 15 seconds of backoff
        self.assertGreater(self.clock.time(), 14.0)

    @patch("agent_hub_host.auth.get_host_access_token")
    @patch("agent_hub_host.auth.get_access_token")
    @patch("agent_hub_host.api.register_machine")
    @patch("agent_hub_host.api.ws_handshake")
    @patch("agent_hub_host.api.safe_http_json")
    def test_agent_poll_error_backoff(self, mock_safe_http, mock_ws_handshake, mock_reg, mock_get_token, mock_get_host_token):
        mock_get_token.return_value = "acc123"
        mock_get_host_token.return_value = "acc123"
        agent = api.HostAgent(clock=self.clock, rng=self.rng, quota_collector=self.quota_collector)
        
        iteration = 0
        def ws_side_effect(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            if iteration >= 5:
                agent.shutdown_requested = True
            raise ConnectionError("WS Failed")
            
        mock_ws_handshake.side_effect = ws_side_effect
        mock_safe_http.side_effect = Exception("Poll 500")
        
        agent.run()
        self.assertEqual(mock_ws_handshake.call_count, 5)
        self.assertGreater(self.clock.time(), 14.0)

    @patch("agent_hub_host.auth.get_host_access_token")
    @patch("agent_hub_host.auth.get_access_token")
    @patch("agent_hub_host.api.register_machine")
    @patch("agent_hub_host.api.ws_handshake")
    @patch("agent_hub_host.api.HostAgent._poll_fallback")
    def test_agent_healthy_ws_resets_backoff(self, mock_poll, mock_ws_handshake, mock_reg, mock_get_token, mock_get_host_token):
        mock_get_token.return_value = "acc123"
        mock_get_host_token.return_value = "acc123"
        agent = api.HostAgent(clock=self.clock, rng=self.rng, quota_collector=self.quota_collector)
        
        iteration = 0
        def ws_side_effect(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            if iteration == 2:
                self.clock.sleep(65.0)
            if iteration >= 4:
                agent.shutdown_requested = True
            raise ConnectionError("WS Failed")
            
        mock_ws_handshake.side_effect = ws_side_effect
        agent.run()
        
        self.assertEqual(mock_ws_handshake.call_count, 4)
        # Sleep sequence:
        # iter 1: 1s backoff
        # iter 2: healthy (65s), backoff resets, 1s backoff
        # iter 3: 1s backoff (because count=1)
        self.assertLess(self.clock.time(), 75.0)


if __name__ == "__main__":
    unittest.main()
