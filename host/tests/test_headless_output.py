import contextlib
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_hub_host import headless_output as capture
from agent_hub_host import private_fs
from agent_hub_host.exit_evidence import terminal_evidence

NATIVE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OFFER = '11111111-2222-4333-8444-555555555555'
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(os.name == 'nt', 'POSIX managed headless capture; Windows keeps existing execution')
class HeadlessOutput(unittest.TestCase):
    def setUp(self):
        temporary = ROOT / '.local' / 'tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.env = {**os.environ, 'APLEXER_SESSION_ID': NATIVE,
                    'APLEXER_TAG': 'task-hub-' + OFFER,
                    'APLEXER_WORKSPACE': str(self.workspace)}
        self.record = dict(native_id=NATIVE, offer_id=OFFER, tag='task-hub-' + OFFER,
                           workspace=str(self.workspace), engine='opencode', mode='headless',
                           incarnation=int(time.time() * 1000) - 100)
        self.path = self.workspace / '.local/hub-headless-output' / (OFFER + '.json')

    def run_child(self, code):
        with open(os.devnull, 'wb') as sink, contextlib.redirect_stdout(sink):
            return capture.run([sys.executable, '-c', code], self.env, 'opencode')

    def test_fast_stdout_stderr_survive_without_any_native_history_or_census(self):
        self.assertEqual(self.run_child("import sys; print('real stdout'); print('real stderr', file=sys.stderr); sys.exit(3)"), 3)
        value = json.loads(self.path.read_text())
        self.assertEqual(value['capture_state'], 'complete')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        # Real consumer: no sampled exit_evidence and no surviving native files.
        result = terminal_evidence(self.record, {'exit': {'code': 3, 'signal': None},
                                                   'finished_at_ms': self.record['incarnation'] + 250},
                                   state_dir=str(self.workspace / 'absent-native-state'))
        self.assertEqual(result['exit_code'], 3)
        self.assertEqual(result['wall_time_ms'], 250)
        self.assertIn('real stdout', result['log_tail'])
        self.assertIn('real stderr', result['log_tail'])

    def test_every_snapshot_redacts_complete_prefix_before_output_forwarding(self):
        writes = []
        original = private_fs.atomic_write

        def checked(path, data, limit):
            value = json.loads(data)
            writes.append(value)
            for secret in ('first-private-fragment', 'middle-private-fragment', 'last-private-fragment'):
                self.assertNotIn(secret, value['log_tail'])
            return original(path, data, limit)

        code = "import os,time; os.write(1,b'Cookie: \\\"first-private-fragment\\n'); time.sleep(.03); os.write(2,b'middle-private-fragment\\n'); time.sleep(.03); os.write(1,b'last-private-fragment\\\"\\nfinished safely\\n')"
        with patch.object(private_fs, 'atomic_write', side_effect=checked):
            self.assertEqual(self.run_child(code), 0)
        self.assertGreaterEqual(len(writes), 4)
        self.assertIn('finished safely', writes[-1]['log_tail'])

    def test_overbudget_source_is_explicitly_withheld(self):
        self.assertEqual(self.run_child("import os; os.write(1,b'ordinary output ' * 6000)"), 0)
        value = json.loads(self.path.read_text())
        self.assertEqual(value['capture_state'], 'over_budget')
        self.assertEqual(value['log_tail'], '')
        self.assertGreater(value['bytes_observed'], capture.MAX_SOURCE_BYTES)
        self.assertEqual(capture.retained(self.record)['capture_state'], 'over_budget')

    def test_stale_or_foreign_or_symlink_capture_never_supplies_evidence(self):
        self.run_child("print('owned output')")
        original = json.loads(self.path.read_text())
        for field, bad in [('native_id', OFFER), ('launch_id', NATIVE), ('tag', 'other'),
                           ('workspace', '/other'), ('engine', 'codex'), ('schema_version', True),
                           ('observed_at_ms', 1), ('capture_state', 'unknown'), ('bytes_observed', True)]:
            with self.subTest(field=field):
                value = {**original, field: bad}
                private_fs.atomic_write(self.path, json.dumps(value).encode(), 32768)
                self.assertIsNone(capture.retained(self.record))
        private_fs.atomic_write(self.path, json.dumps(original).encode(), 32768)
        target = self.workspace / 'capture.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertIsNone(capture.retained(self.record))

    def test_interactive_identity_is_not_wrapped(self):
        for env in ({}, {**self.env, 'APLEXER_TAG': 'hub-' + OFFER},
                    {**self.env, 'APLEXER_SESSION_ID': ''}):
            self.assertIsNone(capture.identity(env, 'opencode'))

    def test_final_capture_replaces_older_sample_without_changing_exit_authority(self):
        self.record['exit_evidence'] = {'log_tail': 'earlier census output'}
        self.run_child("print('actual last output')")
        result = terminal_evidence(self.record, {'exit': {'code': None, 'signal': 9},
                                                'finished_at_ms': self.record['incarnation'] + 500},
                                   state_dir=str(self.workspace / 'missing'))
        self.assertEqual(result['exit_signal'], 9)
        self.assertIsNone(result['exit_code'])
        self.assertIn('actual last output', result['log_tail'])
        self.assertNotIn('earlier census output', result['log_tail'])

    def test_descendant_open_pipe_has_bounded_drain_and_no_fabricated_exit(self):
        wrapper = ('from agent_hub_host.headless_output import run; import os,sys; '
                   'raise SystemExit(run([sys.executable,"-c",sys.argv[1]],dict(os.environ),"opencode"))')
        child = ("import subprocess,sys; "
                 "subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
                 "print('leader exited with descendants',flush=True); raise SystemExit(4)")
        process = None
        try:
            started = time.monotonic()
            process = subprocess.Popen([sys.executable, '-c', wrapper, child],
                                       env={**self.env, 'PYTHONPATH': str(ROOT / 'host')},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            process.wait(timeout=8)
            self.assertEqual(process.returncode, 4)
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual(capture.retained(self.record)['capture_state'], 'children_open')
            self.assertIn('leader exited', capture.retained(self.record)['log_tail'])
        finally:
            # The test owns this isolated group, including the open-pipe child.
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)

    def test_mirrors_nonzero_and_uncatchable_signal_exit_without_inventing_success(self):
        wrapper = ('from agent_hub_host.headless_output import run,exit_like_child; import os,sys; '
                   'exit_like_child(run([sys.executable,"-c",sys.argv[1]],dict(os.environ),"opencode"))')
        for ending, expected in [('raise SystemExit(7)', 7),
                                 ('import os,signal; os.kill(os.getpid(),signal.SIGTERM)', -signal.SIGTERM),
                                 ('import os,signal; os.kill(os.getpid(),signal.SIGKILL)', -signal.SIGKILL)]:
            process = None
            try:
                process = subprocess.Popen([sys.executable, '-c', wrapper, "print('before exit',flush=True); " + ending],
                                           env={**self.env, 'PYTHONPATH': str(ROOT / 'host')},
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                           start_new_session=True)
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, expected, error.decode(errors='replace'))
                self.assertIn('before exit', capture.retained(self.record)['log_tail'])
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
