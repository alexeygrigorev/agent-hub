import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host.agent import Bridge, Journal, RESOURCE_HEARTBEAT_SECONDS


class _StopServe(BaseException):
    pass


class _IdleSocket:
    def __init__(self, clock):
        self.clock = clock
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def recv(self, timeout=None):
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            self.clock[0] += timeout
            raise TimeoutError()
        raise _StopServe()


class _Client:
    def __init__(self, socket):
        self.idle_socket = socket
        self.heartbeats = []
        self.requests = []

    def heartbeat(self, labels, engines=None, capabilities=None, resources=None):
        self.heartbeats.append((labels, engines, capabilities, resources))

    def socket(self, machine_id):
        self.machine_id = machine_id
        return self.idle_socket

    def request(self, method, path, body=None, key=None):
        self.requests.append((method, path, body, key))
        return {}


class ResourceHeartbeatTests(unittest.TestCase):
    def test_idle_socket_wakes_for_bounded_resource_heartbeat_without_offer_polling(self):
        self.assertEqual(RESOURCE_HEARTBEAT_SECONDS, 240)
        clock = [0.0]
        socket = _IdleSocket(clock)
        client = _Client(socket)
        with tempfile.TemporaryDirectory() as temp:
            bridge = Bridge.__new__(Bridge)
            bridge.client = client
            bridge.journal = Journal(Path(temp) / "journal.sqlite")
            bridge.launcher = SimpleNamespace(capabilities=lambda: {"shell": {"headless"}})
            bridge.engines = {"shell"}
            bridge.machine_id = None
            bridge.budget_paused_until = 0
            bridge.budget_safety_paused_until = 0
            bridge.executable = "unused"
            bridge.runner = None

            try:
                with (
                    patch("agent_hub_host.agent.time.monotonic", side_effect=lambda: clock[0]),
                    patch("agent_hub_host.agent.time.time", return_value=1000),
                    patch("agent_hub_host.agent.native_launch.available_memory_mb", return_value=4096),
                    patch("agent_hub_host.agent.native_launch.cpu_load_pct", return_value=12),
                    self.assertRaises(_StopServe),
                ):
                    bridge.serve("machine-1", ["worker"])
            finally:
                bridge.journal.db.close()

        self.assertEqual(len(client.heartbeats), 2)
        self.assertEqual(socket.timeouts[0], RESOURCE_HEARTBEAT_SECONDS)
        self.assertEqual(client.heartbeats[1][3], {
            "mem_available_mb": 4096,
            "observed_at": 1_000_000,
            "cpu_load_pct": 12,
        })
        self.assertNotIn(("GET", "/api/offers", None, None), client.requests)


if __name__ == "__main__":
    unittest.main()
