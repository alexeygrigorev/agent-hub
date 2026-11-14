"""Pure fake ownership state transitions; no real child/proc/probe/library calls."""
import importlib.util
from pathlib import Path
import signal
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('owned_supervisor', Path(__file__).resolve().parents[2] / 'tests' / 'public_native_mail_ci_supervisor.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)
ANCHOR = (1234, 77, 1234, 1234)


class FakeOps:
    def __init__(self, status=0, kid=False, stubborn=False):
        self.clock = 0
        self.status = status
        self.kid = kid
        self.stubborn = stubborn
        self.root_reaped = False
        self.kid_reaped = False
        self.identity_value = ANCHOR
        self.probe_error = None
        self.empty = False
        self.signals = []
        self.changed_after_int = False
    def now(self): return self.clock
    def identity(self): return self.identity_value
    def group_exists(self):
        if self.probe_error: raise self.probe_error
        return not self.empty
    def send(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGINT and self.changed_after_int:
            self.identity_value = (1234, 88, 1234, 1234)
    def drain_until(self, until): self.clock = until
    def reap(self):
        if not self.root_reaped:
            self.root_reaped = True
            return ANCHOR[0], self.status
        if self.kid and not self.kid_reaped:
            if signal.SIGKILL not in self.signals or self.stubborn: return None
            self.kid_reaped = True
            return 2345, signal.SIGKILL
        return 'ECHILD'


class CleanupTests(unittest.TestCase):
    def setUp(self):
        for target in ('ctypes.CDLL', 'subprocess.Popen', 'os.killpg', 'os.waitpid', 'os.waitid'):
            gate = patch(target, side_effect=AssertionError('real runtime forbidden'))
            gate.start(); self.addCleanup(gate.stop)
    def test_nonzero_leader_and_live_kid_get_escalation_and_no_success(self):
        ops = FakeOps(status=256, kid=True)
        result = supervisor.cleanup_owned(ops, ANCHOR, 1)
        self.assertEqual(ops.signals, [signal.SIGINT, signal.SIGKILL])
        self.assertTrue(result['owned_subtree_reaped'])
        self.assertTrue(result['residual_children'])
        self.assertEqual(result['leader_status'], 256)
    def test_leader_closed_after_int_does_not_cancel_kill(self):
        ops = FakeOps(kid=True)
        result = supervisor.cleanup_owned(ops, ANCHOR, 1)
        self.assertTrue(result['owned_subtree_reaped'])
        self.assertEqual(ops.signals, [signal.SIGINT, signal.SIGKILL])
    def test_early_success_with_residual_child_is_not_clean(self):
        result = supervisor.cleanup_owned(FakeOps(kid=True), ANCHOR, 1)
        self.assertEqual(result['leader_status'], 0)
        self.assertTrue(result['residual_children'])
    def test_already_empty_requires_echild_not_probe_alone(self):
        ops = FakeOps(); ops.empty = True
        result = supervisor.cleanup_owned(ops, ANCHOR, 1)
        self.assertTrue(result['owned_subtree_reaped']); self.assertFalse(result['unknown'])
        self.assertEqual(ops.signals, [])
        ops = FakeOps(kid=True); ops.empty = True
        result = supervisor.cleanup_owned(ops, ANCHOR, .3)
        self.assertFalse(result['owned_subtree_reaped']); self.assertTrue(result['unknown'])
    def test_eperm_or_unknown_probe_never_signals_or_counts_empty(self):
        for error in [PermissionError(), OSError(), RuntimeError()]:
            ops = FakeOps(kid=True); ops.probe_error = error
            result = supervisor.cleanup_owned(ops, ANCHOR, .3)
            self.assertTrue(result['unknown']); self.assertEqual(ops.signals, [])
    def test_pid_birth_or_group_change_never_signals_foreign_group(self):
        for identity in [(1234,88,1234,1234),(1234,77,999,1234),(1234,77,1234,999)]:
            ops = FakeOps(); ops.identity_value = identity
            result = supervisor.cleanup_owned(ops, ANCHOR, 1)
            self.assertTrue(result['unknown']); self.assertEqual(ops.signals, [])
        ops = FakeOps(kid=True); ops.changed_after_int = True
        result = supervisor.cleanup_owned(ops, ANCHOR, .3)
        self.assertEqual(ops.signals, [signal.SIGINT]); self.assertTrue(result['unknown'])
    def test_invalid_initial_group_pin_is_not_authority(self):
        for anchor in [(1234,77,999,999),(1234,77.0,1234,1234),None]:
            ops = FakeOps(); result = supervisor.cleanup_owned(ops, anchor, .3)
            self.assertTrue(result['unknown']); self.assertEqual(ops.signals, [])
    def test_expired_deadline_never_probes_or_signals(self):
        ops = FakeOps()
        ops.identity = lambda: self.fail('late identity probe')
        ops.group_exists = lambda: self.fail('late group probe')
        result = supervisor.cleanup_owned(ops, ANCHOR, 0)
        self.assertTrue(result['unknown']); self.assertEqual(ops.signals, [])
    def test_stubborn_kid_and_absolute_deadline_stay_unknown(self):
        ops = FakeOps(kid=True, stubborn=True)
        result = supervisor.cleanup_owned(ops, ANCHOR, .3)
        self.assertTrue(result['unknown']); self.assertFalse(result['owned_subtree_reaped'])
        self.assertLessEqual(ops.clock, .3)
        self.assertEqual(ops.signals, [signal.SIGINT, signal.SIGKILL])

    def test_echild_and_pipe_eof_are_independent_capture_boundaries(self):
        ops = FakeOps()
        ops.pending = True
        ops.capture_pending = lambda: ops.pending
        def drain(until):
            ops.clock = until
            ops.pending = False
        ops.drain_until = drain
        self.assertTrue(supervisor.drain_capture(ops, 1))
        ops.pending = True
        ops.drain_until = lambda until: setattr(ops, 'clock', until)
        self.assertFalse(supervisor.drain_capture(ops, .03))


if __name__ == '__main__': unittest.main()
