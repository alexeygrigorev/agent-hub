"""AUTHORED UNRUN: fixed operator launcher, fake Windows/file/process only."""
import io
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub_host import windows_scoped_claude_enroll as operator
from agent_hub_host import windows_release as release

G = 21


def output(**changes):
    value = {'status': 'resumed', 'provider': 'claude', 'label': 'hub-claude',
             'credential_generation': G, 'acked_generation': G, 'machine_id': 'DESKTOP-TEST'}
    value.update(changes)
    return json.dumps(value).encode()


class Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value


class Child:
    def __init__(self, code=0):
        self.returncode = code
        self.stdout = io.BytesIO(output())
        self.kills, self.waits = 0, []
    def poll(self): return self.returncode
    def wait(self, timeout):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired('synthetic', timeout)
        return self.returncode
    def kill(self): self.kills += 1


class Pump:
    raw = output()
    overflow = failed = False
    done = True
    def __init__(self, pipe): self.pipe = pipe
    def start(self): pass
    def join(self, timeout):
        if self.done:
            self.pipe.close()
        return self.done


class OperatorTests(unittest.TestCase):
    def setUp(self):
        operator._PENDING = None  # Test-only module state; no real ownership exists.
        self.addCleanup(setattr, operator, '_PENDING', None)
        self.clock = Clock()
        self.closed = []
        outer = self
        class File:
            def check(self): pass
            def close(self): outer.closed.append('file')
        self.lease = object.__new__(release.WindowsRelease)
        self.lease.files, self.lease.closed, self.lease.borrowers = [File()], False, 0
        self.lease.owner_thread = release.threading.get_ident()
        self.lease.python, self.lease.archive = 'SELECTED-PYTHON', r'C:\owned\hub.pyz'
        self.child = Child()
        self.calls = []
    def spawn(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertEqual(self.lease.borrowers, 1)
        return self.child
    def run_operation(self, check=False, **kwargs):
        defaults = {'acquire': lambda: self.lease, 'spawn': self.spawn,
                    'pump_factory': Pump, 'clock': self.clock}
        defaults.update(kwargs)
        return operator.operate(check, None if check else G, **defaults)

    def test_no_argument_hold_does_not_acquire_read_config_or_spawn(self):
        with patch.object(operator, '_acquire', side_effect=AssertionError('no acquisition')), \
             patch.object(operator.subprocess, 'Popen', side_effect=AssertionError('no child')), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(operator.main([]), 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'HOLD')

    def test_check_only_real_lease_no_child_credentials_or_provider(self):
        result, budget = self.run_operation(True, spawn=lambda *a, **k: self.fail('spawn forbidden'))
        self.assertEqual(result, {'status': 'capsule_ready', 'credentials_or_provider_actions': False})
        self.assertTrue(self.lease.closed)
        self.assertEqual(self.closed, ['file'])
        self.assertEqual(budget.deadline - budget.start, operator.CHECK_SECONDS)

    def test_fixed_resume_argv_stream_bounds_and_auth_environment_unchanged(self):
        result, budget = self.run_operation()
        self.assertEqual(self.calls[0][0], [self.lease.python, '-I', '-S', self.lease.archive,
            'engines', 'import-native', '--provider', 'claude', '--label', 'hub-claude',
            '--resume-generation', str(G)])
        options = self.calls[0][1]
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stdout'], subprocess.PIPE)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertTrue(options['close_fds'])
        self.assertNotIn('env', options)  # Never erase worker/operator safety guards.
        self.assertEqual(result['status'], 'resume_completed')
        self.assertEqual(result['descendant_custody'], 'unproven')
        self.assertEqual(result['qualification'], 'fixed_cli_result_only')
        self.assertNotIn('machine_id', result)
        self.assertTrue(self.child.waits)
        self.assertTrue(self.lease.closed)
        self.assertEqual(budget.deadline - budget.start, operator.OPERATOR_SECONDS)

    def test_nonzero_operator_auth_or_generation_failure_has_no_success(self):
        self.child.returncode = 3
        with self.assertRaises(operator.Refused) as caught: self.run_operation()
        self.assertEqual(caught.exception.args, ('operator_failed',))
        self.assertTrue(self.lease.closed)
        self.assertIsNone(operator._PENDING)

    def test_forged_or_extra_cli_result_and_changed_generation_refuse(self):
        variants = [output(status='imported'), output(acked_generation=G+1),
                    output(credential_generation=True), output(label='other'), output(secret='never print')]
        for raw in variants:
            with self.subTest(raw_kind=len(raw)):
                with self.assertRaises(operator.Refused): operator._result(raw, G)
        duplicate = output()[:-1] + b',"status":"resumed"}'
        with self.assertRaises(operator.Refused): operator._result(duplicate, G)

    def test_raw_cli_text_and_secret_never_escape_main(self):
        original = RuntimeError('synthetic-secret-token-and-path')
        with patch.object(operator, 'operate', side_effect=original), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(operator.main(['--generation', str(G)]), 1)
        value = json.loads(out.getvalue())
        self.assertEqual(value['status'], 'UNKNOWN')
        self.assertFalse(value['automatic_retry'])
        self.assertNotIn('secret', out.getvalue())

    def test_ambiguous_spawn_keeps_pending_and_original_control(self):
        original = KeyboardInterrupt('synthetic-control')
        def spawn(*a, **k): raise original
        with self.assertRaises(KeyboardInterrupt) as caught: self.run_operation(spawn=spawn)
        self.assertIs(caught.exception, original)
        self.assertIs(original.pending, operator._PENDING)
        self.assertIs(operator._PENDING.lease, self.lease)
        self.assertEqual(self.lease.borrowers, 1)
        self.assertFalse(self.lease.closed)
        with self.assertRaises(release.Refused): self.lease.close()

    def test_unreaped_timeout_keeps_caller_lifetime_pending_and_no_retry(self):
        self.child.returncode = None
        def sleep(seconds): self.clock.value += 421
        with self.assertRaises(operator.Refused): self.run_operation(sleep=sleep)
        self.assertEqual(self.child.kills, 1)
        self.assertIs(operator._PENDING.child, self.child)
        self.assertFalse(self.lease.closed)
        self.assertEqual(self.lease.borrowers, 1)
        with self.assertRaises(operator.Refused): self.run_operation()
        self.assertEqual(len(self.calls), 1)

    def test_timeout_reaped_leader_is_unknown_not_descendant_closure(self):
        self.child.returncode = None
        def kill():
            self.child.kills += 1
            self.child.returncode = -9
        self.child.kill = kill
        def sleep(seconds): self.clock.value += 421
        with self.assertRaises(operator.Refused): self.run_operation(sleep=sleep)
        self.assertTrue(self.lease.closed)
        self.assertIsNone(operator._PENDING)
        self.assertEqual(self.child.returncode, -9)

    def test_reader_unsettled_with_reaped_leader_retains_lease(self):
        class Stalled(Pump): done = False
        with self.assertRaises(operator.Refused): self.run_operation(pump_factory=Stalled)
        self.assertIs(operator._PENDING.child, self.child)
        self.assertFalse(self.lease.closed)

    def test_cli_pending_explicitly_reports_owner_exit_loss_without_durable_claim(self):
        operator._PENDING = operator.Pending(self.lease, self.child, None)
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(operator.main(['--generation', str(G)]), 1)
        value = json.loads(out.getvalue())
        self.assertTrue(value['owner_exit_custody_loss'])
        self.assertEqual(value['retention'], 'owning_process_lifetime_only')
        self.assertEqual(value['descendant_custody'], 'unproven')
        self.assertNotIn('lease', out.getvalue())

    def test_late_close_cannot_qualify_capsule_or_resume_success(self):
        outer = self
        class Slow:
            def check(self): pass
            def close(self): outer.clock.value += 421
        self.lease.files = [Slow()]
        with self.assertRaises(operator.Refused): self.run_operation(True)
        self.assertTrue(self.lease.closed)

    def test_backward_or_invalid_clock_refuses_without_spawn(self):
        for bad in (99.0, float('nan')):
            with self.subTest(kind=str(bad)):
                values = iter((100.0, bad))
                with self.assertRaises(operator.Refused):
                    operator.Budget(20, lambda: next(values)).remaining()
        self.assertEqual(self.calls, [])

    def test_source_changed_after_real_child_result_refuses(self):
        outer = self
        class Source:
            count = 0
            def check(self):
                self.count += 1
                if self.count == 2: raise release.Refused('source changed')
            def close(self): outer.closed.append('source')
        self.lease.files = [Source()]
        with self.assertRaises(release.Refused): self.run_operation()
        self.assertTrue(self.lease.closed)

    def test_invalid_flags_cannot_select_arbitrary_provider_path_or_argv(self):
        for argv in (['--generation','0'], ['--generation','1','--engine','go'],
                     ['--generation','true'], ['--check-only','--generation','1'],
                     ['--archive','foreign'], ['--generation',str(2**53)]):
            with self.subTest(flag=argv[0]):
                with self.assertRaises(operator.Refused): operator._parse(argv)
        self.assertEqual(operator._parse(['--check-only']), (True, None))
        self.assertEqual(operator._parse(['--generation',str(G)]), (False, G))

    def test_actual_bounded_pump_caps_memory_and_closes_owned_pipe(self):
        pipe = io.BytesIO(b'x' * (operator.OUTPUT_BYTES * 4))
        pump = operator._Pump(pipe)
        pump._read()  # Pure in-memory read algorithm; do not start a process/thread.
        self.assertTrue(pump.overflow)
        self.assertLessEqual(len(pump.raw), operator.OUTPUT_BYTES + 1)
        self.assertTrue(pipe.closed)

    def test_pump_construction_failure_closes_pipe_when_child_settled(self):
        pipe = self.child.stdout
        def refuse(pipe): raise RuntimeError('synthetic-pump')
        with self.assertRaises(RuntimeError): self.run_operation(pump_factory=refuse)
        self.assertTrue(pipe.closed)
        self.assertIsNone(self.child.stdout)
        self.assertTrue(self.lease.closed)

    def test_overflow_never_yields_a_valid_cli_result(self):
        class Overflow(Pump):
            overflow = True
            raw = b'x' * (operator.OUTPUT_BYTES + 1)
        with self.assertRaises(operator.Refused): self.run_operation(pump_factory=Overflow)
        self.assertTrue(self.lease.closed)

    def test_late_resume_close_refuses_even_after_valid_cli_result(self):
        outer = self
        class Slow:
            def check(self): pass
            def close(self): outer.clock.value += 421
        self.lease.files = [Slow()]
        with self.assertRaises(operator.Refused): self.run_operation()
        self.assertTrue(self.lease.closed)
        self.assertIsNone(operator._PENDING)

    def test_main_fresh_final_deadline_check_cannot_print_stale_success(self):
        budget = SimpleNamespace(remaining=lambda: (_ for _ in ()).throw(operator.Refused('operation_deadline')))
        with patch.object(operator, 'operate', return_value=({'status': 'resume_completed'}, budget)), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(operator.main(['--generation', str(G)]), 1)
        self.assertEqual(json.loads(out.getvalue())['status'], 'UNKNOWN')
        self.assertNotIn('resume_completed', out.getvalue())
