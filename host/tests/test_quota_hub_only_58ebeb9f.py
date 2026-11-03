import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agent_hub_host.api import HostAgent


class TestHubOnlyQuotaCollection(unittest.TestCase):
    def test_host_agent_has_no_machine_quota_collector_or_tick(self):
        with patch("agent_hub_host.api._host_cfg", return_value={}):
            agent = HostAgent(clock=Mock(), dispatcher=Mock())
        self.assertFalse(hasattr(agent, "quota_collector"))
        self.assertFalse(hasattr(agent, "_tick_quota"))

    def test_host_agent_rejects_quota_collector_injection(self):
        collector = Mock()
        with patch("agent_hub_host.api._host_cfg", return_value={}):
            with self.assertRaises(TypeError):
                HostAgent(clock=Mock(), dispatcher=Mock(), quota_collector=collector)
        collector.step.assert_not_called()


if __name__ == "__main__":
    unittest.main()
