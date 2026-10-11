import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
from unittest.mock import patch, MagicMock
from agent_hub_host import gate

class MockClock:
    def __init__(self, time):
        self._time = time
    def time(self):
        return self._time

class TestGate(unittest.TestCase):
    def setUp(self):
        gate._gate_cache = None
        gate._gate_cache_time = 0.0

    @patch("os.path.exists")
    def test_no_launcher(self, mock_exists):
        mock_exists.return_value = False
        res = gate.get_gate_status(MockClock(100.0))
        self.assertFalse(res["launch_allowed"])
        self.assertEqual(res["reason"], "no launcher")

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_gate_cache(self, mock_run, mock_exists):
        mock_exists.return_value = True
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"launch_allowed": true, "reason": "ok"}'
        
        # First call hits subprocess
        res1 = gate.get_gate_status(MockClock(100.0))
        self.assertTrue(res1["launch_allowed"])
        self.assertEqual(mock_run.call_count, 1)
        
        # Second call within 300s uses cache
        res2 = gate.get_gate_status(MockClock(200.0))
        self.assertTrue(res2["launch_allowed"])
        self.assertEqual(mock_run.call_count, 1)
        
        # Third call after 300s hits subprocess again
        res3 = gate.get_gate_status(MockClock(500.0))
        self.assertTrue(res3["launch_allowed"])
        self.assertEqual(mock_run.call_count, 2)
        
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_gate_sanitization(self, mock_run, mock_exists):
        mock_exists.return_value = True
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"launch_allowed": true, "reason": "minimum available window 95% remaining"}'
        
        res = gate.get_gate_status(MockClock(100.0))
        self.assertTrue(res["launch_allowed"])
        self.assertEqual(res["reason"], "quota ok")

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_gate_failure(self, mock_run, mock_exists):
        mock_exists.return_value = True
        mock_run.return_value.returncode = 1
        
        res = gate.get_gate_status(MockClock(100.0))
        self.assertFalse(res["launch_allowed"])
        self.assertEqual(res["reason"], "failed")

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_gate_75_and_strict_bool(self, mock_run, mock_exists):
        mock_exists.return_value = True
        mock_run.return_value.returncode = 75
        mock_run.return_value.stdout = '{"launch_allowed": "true", "reason": "quota low"}'
        
        res = gate.get_gate_status(MockClock(100.0))
        self.assertFalse(res["launch_allowed"])
        self.assertEqual(res["reason"], "quota low")

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_gate_malformed(self, mock_run, mock_exists):
        mock_exists.return_value = True
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = 'invalid json'
        
        res = gate.get_gate_status(MockClock(100.0))
        self.assertFalse(res["launch_allowed"])
        self.assertEqual(res["reason"], "malformed")
