"""Pure coverage of the e2e-only capacity seam; no server or worker starts."""
import importlib.util
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import resource_admission
from agent_hub_host.agent import Journal

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('resource_fixture_harness', REPO / 'hub2/tests/e2e/harness.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class ResourceFixtureTests(unittest.TestCase):
    def test_default_invocation_and_production_sampler_are_unchanged(self):
        original = resource_admission.available_bytes
        self.assertEqual(harness.host_command('/owned/config.json'), [sys.executable, '-m',
            'agent_hub_host.cli', '--config', '/owned/config.json', 'serve', '--label', 'e2e'])
        self.assertIs(resource_admission.available_bytes, original)

    def test_explicit_bootstrap_keeps_cli_arguments_and_real_reservation_checks(self):
        parent = REPO / '.local/tmp'
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        original = resource_admission.available_bytes
        with tempfile.TemporaryDirectory(dir=parent) as name:
            journal = Journal(Path(name) / 'journal.sqlite')
            self.addCleanup(journal.close_thread)
            try:
                with journal.db:
                    for i in range(10):
                        op, sid = 'operation-' + str(i), 'session-' + str(i)
                        journal.db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,state,native_dispatch_started) VALUES(?,?,1,?,?,?,\'launching\',0)',
                            (op, sid, 'digest', name, 'task-hub2-' + sid))
                        row = journal.db.execute('SELECT * FROM launches WHERE operation_id=?', (op,)).fetchone()
                        journal.db.execute('UPDATE launches SET admission_json=? WHERE operation_id=?',
                                           (resource_admission.starting(row), op))
                command = harness.host_command('/owned/config.json', 64 * 1024**3)
                seen = []
                def cli(module, **kwargs):
                    seen.append((module, kwargs, sys.argv[1:]))
                    self.assertEqual(resource_admission.available_bytes(), 64 * 1024**3)
                    for i in range(10):
                        resource_admission.admit(journal.db, 'operation-' + str(i), 'session-' + str(i), 1)
                    self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM launches WHERE native_dispatch_started=1').fetchone()[0], 10)
                with patch.object(sys, 'argv', ['-c', *command[3:]]), patch.object(runpy, 'run_module', side_effect=cli), \
                        patch.object(resource_admission, 'available_bytes', original):
                    exec(command[2], {'__name__': '__main__'})
                self.assertEqual(seen, [('agent_hub_host.cli', {'run_name': '__main__', 'alter_sys': True},
                    ['--config', '/owned/config.json', 'serve', '--label', 'e2e'])])
            finally:
                journal.close_thread()
        self.assertIs(resource_admission.available_bytes, original)

    def test_host_start_passes_opt_in_only_to_owned_fixture_child(self):
        parent = REPO / '.local/tmp'
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as name:
            host = harness.Host.__new__(harness.Host)
            host.root = Path(name)
            host.config = host.root / 'config.json'
            host.log_path = host.root / 'fixture.log'
            host.env = lambda: {'FIXTURE': 'owned'}
            with patch.object(harness.subprocess, 'Popen') as spawn:
                host.start()
            call = spawn.call_args
            try:
                self.assertEqual(call.args[0], harness.host_command(host.config, harness.STUB_HOST_AVAILABLE_BYTES))
                self.assertEqual(call.kwargs['env'], {'FIXTURE': 'owned'})
                self.assertEqual(call.kwargs['cwd'], host.root)
                self.assertTrue(call.kwargs['start_new_session'])
            finally:
                call.kwargs['stdout'].close()

    def test_injected_low_capacity_still_refuses_through_production_admission(self):
        parent = REPO / '.local/tmp'
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as name:
            journal = Journal(Path(name) / 'journal.sqlite')
            try:
                with journal.db:
                    journal.db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,state,native_dispatch_started) VALUES('op','sid',1,'digest',?,'task-hub2-sid','launching',0)", (name,))
                    row = journal.db.execute('SELECT * FROM launches').fetchone()
                    journal.db.execute('UPDATE launches SET admission_json=?', (resource_admission.starting(row),))
                command = harness.host_command('/owned/config.json', 3 * 1024**3)
                def cli(*args, **kwargs):
                    with self.assertRaises(resource_admission.Refused):
                        resource_admission.admit(journal.db, 'op', 'sid', 1)
                with patch.object(sys, 'argv', ['-c', *command[3:]]), patch.object(runpy, 'run_module', side_effect=cli), \
                        patch.object(resource_admission, 'available_bytes', resource_admission.available_bytes):
                    exec(command[2], {'__name__': '__main__'})
                self.assertEqual(journal.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0], 0)
            finally:
                journal.close_thread()

    def test_bad_synthetic_capacity_is_refused_before_any_spawn(self):
        for value in [True, -1, 1.5, 'ample', 2**63]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                harness.host_command('/owned/config.json', value)


if __name__ == '__main__':
    unittest.main()
