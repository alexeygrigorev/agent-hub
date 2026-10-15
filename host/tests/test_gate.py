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
        # The unpatched, genuine proof is exercised in TestGateOperationalProof.
        p = patch("agent_hub_host.runtime.operational_root_profile_proof", return_value=(False, "unproven"))
        p.start()
        self.addCleanup(p.stop)

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
        self.assertFalse(res1["launch_allowed"])
        self.assertEqual(res1["reason"], "pending#59")
        self.assertEqual(mock_run.call_count, 1)
        
        # Second call within 300s uses cache
        res2 = gate.get_gate_status(MockClock(200.0))
        self.assertFalse(res2["launch_allowed"])
        self.assertEqual(mock_run.call_count, 1)
        
        # Third call after 300s hits subprocess again
        res3 = gate.get_gate_status(MockClock(500.0))
        self.assertFalse(res3["launch_allowed"])
        self.assertEqual(mock_run.call_count, 2)
        
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_gate_sanitization(self, mock_run, mock_exists):
        mock_exists.return_value = True
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"launch_allowed": true, "reason": "minimum available window 95% remaining"}'
        
        res = gate.get_gate_status(MockClock(100.0))
        self.assertFalse(res["launch_allowed"])
        self.assertEqual(res["reason"], "pending#59")
        

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


class TestGateOperationalProof(unittest.TestCase):
    """pending#59 lifts only on a genuine operational Root profile proof; no caller input."""

    def setUp(self):
        gate._gate_cache = None
        gate._gate_cache_time = 0.0

    def _run(self, proof, quota='{"launch_allowed": true, "reason": "minimum available window 95% remaining"}'):
        with patch("os.path.exists", return_value=True), patch("subprocess.run") as run, proof:
            run.return_value.returncode = 0
            run.return_value.stdout = quota
            return gate.get_gate_status(MockClock(100.0))

    def test_genuine_proof_lifts_pending_59_and_keeps_the_sanitized_reason(self):
        res = self._run(patch("agent_hub_host.runtime.operational_root_profile_proof",
                              return_value=(True, "root_profile_proven")))
        self.assertTrue(res["launch_allowed"])
        self.assertEqual(res["reason"], "quota ok")

    def test_unproven_missing_sdk_or_exception_stays_denied(self):
        for proof in (patch("agent_hub_host.runtime.operational_root_profile_proof", return_value=(False, "x")),
                      patch("agent_hub_host.runtime.operational_root_profile_proof", side_effect=ImportError("no launcher")),
                      patch("agent_hub_host.runtime.operational_root_profile_proof", side_effect=RuntimeError("boom"))):
            gate._gate_cache = None
            res = self._run(proof)
            self.assertFalse(res["launch_allowed"])
            self.assertEqual(res["reason"], "pending#59")

    def test_quota_refusal_is_never_lifted_by_the_proof(self):
        res = self._run(patch("agent_hub_host.runtime.operational_root_profile_proof",
                              return_value=(True, "root_profile_proven")),
                        quota='{"launch_allowed": false, "reason": "quota low"}')
        self.assertFalse(res["launch_allowed"])

    def test_real_proof_on_this_machine_is_denied_because_pins_and_boundary_are_absent(self):
        res = self._run(patch("os.path.isdir", return_value=True))
        self.assertFalse(res["launch_allowed"])
        self.assertEqual(res["reason"], "pending#59")

    def test_there_is_no_caller_supplied_root_or_profile_input(self):
        with self.assertRaises(TypeError):
            gate.get_gate_status(MockClock(1.0), root=True)
        with self.assertRaises(TypeError):
            gate.get_gate_status(MockClock(1.0), profile_supported=True)
