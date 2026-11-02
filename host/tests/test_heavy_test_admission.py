"""Real subprocess/lock tests; private roots, no host load generation."""
import ast
import errno
import fcntl
import importlib.util
import io
import json
from unittest import mock
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

WRAPPER = Path(__file__).resolve().parents[2] / 'scripts/capped-test-run.sh'


def _admission_module():
    spec = importlib.util.spec_from_file_location('heavy_admission', WRAPPER.with_name('heavy-test-admission.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        parent = WRAPPER.parent.parent / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.children = []
        self.env = dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=str(self.root),
                        AGENT_HUB_HEAVY_TEST_SLOTS='1', AGENT_HUB_HEAVY_TEST_NORMAL_AGE_S='300', AGENT_HUB_HEAVY_TEST_WAIT_S='40',
                        AGENT_HUB_HEAVY_TEST_PRIORITY='normal')
        # The full-suite retry wrapper pins one run id to its own ticket. These
        # subprocess tests exercise independent tickets in a private pool.
        self.env.pop('AGENT_HUB_HEAVY_TEST_RUN_ID', None)
        self.env.pop('AGENT_HUB_HEAVY_TEST_HOLDER_TOKEN', None)
        # The full-suite runner pins one run id so its separate discovery
        # phases share a FIFO ticket. These tests exercise fresh admissions
        # and must not inherit that cross-phase identity.
        self.env.pop('AGENT_HUB_HEAVY_TEST_RUN_ID', None)
        self.holder = (self.root / 'slot-0.lock').open('a+')
        fcntl.flock(self.holder, fcntl.LOCK_EX)

    def tearDown(self):
        self.holder.close()
        # Only this test's subprocess groups, including failed assertions.
        for child in self.children:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        self.temp.cleanup()

    def until(self, condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail('timed out waiting for test event')
            time.sleep(.02)

    def start(self, name, priority='normal', code=None, wait='40', run_timeout=None):
        count = self.state()['next']
        code = code or f"from pathlib import Path; p=Path({str(self.root / 'order')!r}); f=p.open('a'); f.write({name!r}+'\\n'); f.close()"
        command = [str(WRAPPER)]
        if hasattr(self, 'scheduler_clock_origin'):
            # Keep a shared positive clock epoch even on freshly booted CI VMs.
            # Elapsed time and sleep remain real; only this fixture's epoch moves.
            helper = WRAPPER.with_name('heavy-test-admission.py')
            bootstrap = (
                'import runpy,sys,time; real_monotonic=time.monotonic; '
                f'time.monotonic=lambda:1000+real_monotonic()-{self.scheduler_clock_origin!r}; '
                f'sys.argv=[{str(helper)!r}]+sys.argv[1:]; '
                'runpy.run_path(sys.argv[0],run_name="__main__")'
            )
            command = [sys.executable, '-c', bootstrap]
        args = command + ['--priority', priority, '--wait', wait]
        if run_timeout is not None:
            args += ['--run-timeout', str(run_timeout)]
        child = subprocess.Popen(args + [
                                  '--', sys.executable, '-c', code], env=self.env,
                                 start_new_session=True)
        self.children.append(child)
        self.until(lambda: self.state()['next'] > count or child.poll() is not None)
        return child

    def state(self):
        try:
            return json.loads((self.root / 'fifo-state.json').read_text())
        except FileNotFoundError:
            return {'next': 1, 'active': {}}

    def order(self):
        return (self.root / 'order').read_text().splitlines()

    def test_fifo(self):
        children = [self.start(str(i)) for i in range(5)]
        self.holder.close()
        self.assertEqual([p.wait(timeout=40) for p in children], [0]*5)
        self.assertEqual(self.order(), list(map(str, range(5))))

    def test_priority_fifo_without_preemption(self):
        children = [self.start('normal1'), self.start('final1', 'final'),
                    self.start('normal2'), self.start('final2', 'final')]
        self.assertFalse((self.root / 'order').exists())
        self.holder.close()
        self.assertEqual([p.wait(timeout=40) for p in children], [0]*4)
        self.assertEqual(self.order(), ['final1', 'final2', 'normal1', 'normal2'])

    def test_release_precedes_final_and_normal_without_preemption(self):
        # Intake 1293b560 AC3: the orchestrator-main release gate lane is
        # admitted ahead of both final and normal worker lanes.
        children = [self.start('normal1'), self.start('final1', 'final'),
                    self.start('release1', 'release'),
                    self.start('normal2'), self.start('final2', 'final')]
        self.assertFalse((self.root / 'order').exists())
        self.holder.close()
        self.assertEqual([p.wait(timeout=40) for p in children], [0]*5)
        self.assertEqual(self.order(), ['release1', 'final1', 'final2', 'normal1', 'normal2'])

    def test_release_env_priority_is_admitted_as_release(self):
        env = dict(self.env, AGENT_HUB_HEAVY_TEST_PRIORITY='release')
        child = subprocess.Popen([str(WRAPPER), '--wait', '40', '--',
                                  sys.executable, '-c',
                                  f"from pathlib import Path; Path({str(self.root / 'order')!r}).write_text('rel')"],
                                 env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        self.children.append(child)
        self.holder.close()
        stdout, stderr = child.communicate(timeout=40)
        self.assertEqual(child.returncode, 0, stderr.decode(errors='replace'))
        self.assertIn('admitted release ticket', stderr.decode(errors='replace'))
        self.assertEqual((self.root / 'order').read_text(), 'rel')

    def test_slots_file_is_authoritative_over_env(self):
        # Intake 1293b560 AC2: the live <dir>/slots file is the single
        # host-level setting; a per-process env value never overrides it.
        holders = [self.holder]
        for index in range(1, 9):
            handle = (self.root / f'slot-{index}.lock').open('a+')
            fcntl.flock(handle, fcntl.LOCK_EX)
            holders.append(handle)
        try:
            (self.root / 'slots').write_text('1\n')
            env = dict(self.env, AGENT_HUB_HEAVY_TEST_SLOTS='9')
            result = subprocess.run([str(WRAPPER), '--wait', '0', '--',
                                     sys.executable, '-c', 'pass'],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn('(1 slots)', result.stderr)
            # No file: the env value remains the fallback for private pools
            # (clamped to the MAX_SLOTS ceiling).
            (self.root / 'slots').unlink()
            result = subprocess.run([str(WRAPPER), '--wait', '0', '--',
                                     sys.executable, '-c', 'pass'],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn('(8 slots)', result.stderr)
        finally:
            for handle in holders[1:]:
                handle.close()

    def test_aged_waiter_cannot_be_overtaken_by_new_finals(self):
        self.scheduler_clock_origin = time.monotonic()
        normal = self.start('normal')
        first_final = self.start('final1', 'final')
        # Advance only this fixture's normal registration age, under the same
        # mutex used by the real scheduler, rather than waiting for the aging window.
        with (self.root / 'admission.lock').open('a+') as mutex:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            state = self.state()
            for row in state['active'].values():
                if row['priority'] == 'normal':
                    row['created'] -= 301
                    self.assertGreaterEqual(row['created'], 0)
            staging = self.root / 'fifo-state.next'
            staging.write_text(json.dumps(state))
            os.replace(staging, self.root / 'fifo-state.json')
        later_finals = [self.start(f'final{i}', 'final') for i in range(2, 5)]
        self.holder.close()
        self.assertEqual([p.wait(timeout=40) for p in [normal, first_final] + later_finals], [0]*5)
        self.assertEqual(self.order(), ['normal', 'final1', 'final2', 'final3', 'final4'])

    def test_real_admissions_reuse_pool_and_advance_counter(self):
        self.holder.close()
        for i in range(20):
            self.assertEqual(self.start(str(i)).wait(timeout=40), 0)
        self.assertEqual(self.state()['next'], 21)
        self.assertEqual(self.state()['active'], {})
        self.assertEqual(len(list((self.root / 'leases').iterdir())), 1)

    def test_crashed_waiter_skipped_without_deleting_ticket(self):
        dead = self.start('dead', 'final')
        lease_path = self.root / 'leases/0.lock'
        original_inode = lease_path.stat().st_ino
        dead.kill()
        dead.wait()
        survivor = self.start('survivor')
        self.holder.close()
        self.assertEqual(survivor.wait(timeout=40), 0)
        self.assertEqual(self.order(), ['survivor'])
        self.assertEqual(lease_path.stat().st_ino, original_inode)
        self.assertEqual(self.state()['next'], 3)
        self.assertEqual(len(list((self.root / 'leases').iterdir())), 1)

    def test_crashed_holder_releases_slot(self):
        self.holder.close()
        marker = self.root / 'started'
        holder = self.start('holder', code=f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30)")
        self.until(marker.exists)
        waiter = self.start('next')
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait()
        self.assertEqual(waiter.wait(timeout=40), 0)
        self.assertEqual(self.order(), ['next'])

    def test_overdue_unbounded_holder_is_reclaimed_by_waiter(self):
        self.holder.close()
        stub = self.root / 'stub' / 'heavy-test-admission.py'
        stub.parent.mkdir()
        stub.write_text(
            'import fcntl,subprocess,sys,time\n'
            f'h=open({str(self.root / "slot-0.lock")!r},"a+"); fcntl.flock(h,fcntl.LOCK_EX)\n'
            'c=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"],start_new_session=True)\n'
            f'open({str(self.root / "stub-started")!r},"w").close()\n'
            'c.wait()\n')
        env = {k: v for k, v in self.env.items() if k != 'AGENT_HUB_HEAVY_TEST_RUN_TIMEOUT_S'}
        old = subprocess.Popen([sys.executable, str(stub)], env=env, start_new_session=True)
        self.children.append(old)
        self.until((self.root / 'stub-started').exists)
        module = _admission_module()
        module.OVERDUE_HOLDER_S = 1
        module.TERMINATE_GRACE_S = 2
        time.sleep(1.2 + 0.1)
        # Younger than the threshold: untouched.
        module.OVERDUE_HOLDER_S = 3600
        module.reap_overdue_holders(self.root, 1)
        self.assertIsNone(old.poll())
        module.OVERDUE_HOLDER_S = 1
        module.reap_overdue_holders(self.root, 1)
        self.assertEqual(old.wait(timeout=10), 0)
        self.assertEqual(self.start('after').wait(timeout=20), 0)

    def test_run_timeout_terminates_command_and_releases_slot(self):
        self.holder.close()
        marker = self.root / 'started'
        code = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30)"
        timed = self.start('timed', code=code, run_timeout=1)
        self.until(marker.exists)
        self.assertEqual(timed.wait(timeout=5), 124)
        next_run = self.start('next')
        self.assertEqual(next_run.wait(timeout=5), 0)
        self.assertEqual(self.order(), ['next'])

    def test_expired_direct_pytest_holder_stops_the_test_process(self):
        hook_root = self.root / 'hook-root'
        hook_root.mkdir()
        conftest = WRAPPER.parent.parent / 'host/tests/conftest.py'
        code = (
            'import importlib.util,time; '
            f'spec=importlib.util.spec_from_file_location("hub_test_conftest",{str(conftest)!r}); '
            'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); '
            'from types import SimpleNamespace; module.pytest_configure(SimpleNamespace()); '
            'time.sleep(30)'
        )
        env = dict(self.env, AGENT_HUB_HEAVY_TEST_DIR=str(hook_root),
                   AGENT_HUB_HEAVY_TEST_RUN_TIMEOUT_S='1')
        env.pop('AGENT_HUB_HEAVY_TEST_HELD', None)
        env.pop('CI', None)
        child = subprocess.Popen([sys.executable, '-c', code], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        self.children.append(child)
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, -signal.SIGTERM,
                         (stdout.decode(errors='replace'), stderr.decode(errors='replace')))
        self.assertIn(b'stopping this test process', stderr)

    def test_timeout_and_exit_status(self):
        timed = self.start('timeout', wait='0')
        self.assertEqual(timed.wait(timeout=40), 3)
        self.holder.close()
        child = self.start('exit', code='raise SystemExit(17)')
        self.assertEqual(child.wait(timeout=40), 17)

    def test_term_forwards_to_group_and_holds_slot_until_group_exits(self):
        self.holder.close()
        marker = self.root / 'started'
        term_marker = self.root / 'child-term'
        child_ready = self.root / 'child-ready'
        child_pid = self.root / 'child.pid'
        child_code = (
            'import signal,sys,time; from pathlib import Path; '
            f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(term_marker)!r}).touch(), "
            'time.sleep(1),sys.exit(0))); '
            f'Path({str(child_ready)!r}).touch(); time.sleep(300)'
        )
        code = f'''import signal,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable, '-c', {child_code!r}], pass_fds=(9,))
def stop(*_):
    child.wait()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path({str(child_pid)!r}).write_text(str(child.pid))
Path({str(marker)!r}).touch()
time.sleep(30)
'''
        holder = self.start('holder', code=code)
        self.until(lambda: marker.exists() and child_pid.exists() and child_ready.exists())
        descendant = int(child_pid.read_text())
        signal_started = time.monotonic()
        holder.terminate()
        waiter = self.start('next')
        self.until(lambda: term_marker.exists(), timeout=5)
        self.assertLess(time.monotonic() - signal_started, 5)
        self.assertTrue(Path(f'/proc/{descendant}/stat').exists(),
                        'descendant exited before its SIGTERM handler completed')
        # The waiter cannot pass admission while the holder's process group
        # still owns the inherited slot descriptor, including while the child
        # is deliberately delayed in its SIGTERM handler.
        self.assertFalse((self.root / 'order').exists())
        self.assertEqual(holder.wait(timeout=40), 143)
        self.assertEqual(waiter.wait(timeout=40), 0)
        self.assertEqual(self.order(), ['next'])
        self.until(lambda: self.process_state(descendant) in (None, 'Z'))

    @staticmethod
    def process_state(pid):
        try:
            stat = Path(f'/proc/{pid}/stat').read_text()
        except FileNotFoundError:
            return None
        close = stat.rfind(')')
        return stat[close + 1:].split()[0] if close >= 0 else None

    def test_nested_admission_after_unsetting_held_flag_fails_fast(self):
        self.holder.close()
        helper = WRAPPER.with_name('heavy-test-admission.py')
        nested = (
            f"unset AGENT_HUB_HEAVY_TEST_HELD; exec {sys.executable} {str(helper)!r} "
            "--slots 1 --wait 30 -- python -c 'pass'"
        )
        started = time.monotonic()
        outer = subprocess.Popen(
            [sys.executable, str(helper), '--slots', '1', '--wait', '30', '--',
             'bash', '-c', nested], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.children.append(outer)
        try:
            stdout, stderr = outer.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(outer.pid, signal.SIGKILL)
            outer.wait()
            self.fail('nested admission waited for another heavy-test slot')
        self.assertEqual(outer.returncode, 2,
                         (stdout.decode(errors='replace'), stderr.decode(errors='replace')))
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn(b'nested heavy-test admission detected', stderr)
        self.assertIn(b'already owns a slot', stderr)

    def test_nested_admission_with_different_pool_is_allowed(self):
        self.holder.close()
        helper = WRAPPER.with_name('heavy-test-admission.py')
        other_pool = self.root / 'other-pool'
        other_pool.mkdir()
        nested = (
            f"AGENT_HUB_HEAVY_TEST_DIR={str(other_pool)!r} {sys.executable} {str(helper)!r} "
            "--slots 1 --wait 0 -- python -c 'print(\"different-pool-ran\")'"
        )
        outer = subprocess.Popen(
            [sys.executable, str(helper), '--slots', '1', '--wait', '30', '--',
             'bash', '-c', nested], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.children.append(outer)
        try:
            stdout, stderr = outer.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(outer.pid, signal.SIGKILL)
            outer.wait()
            self.fail('nested admission against a different pool did not finish')
        self.assertEqual(outer.returncode, 0,
                         (stdout.decode(errors='replace'), stderr.decode(errors='replace')))
        self.assertIn(b'different-pool-ran', stdout)


class RefusalTests(unittest.TestCase):
    def setUp(self):
        parent = WRAPPER.parent.parent / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.state_path = self.root / 'fifo-state.json'
        self.env = dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=str(self.root),
                        AGENT_HUB_HEAVY_TEST_SLOTS='1', AGENT_HUB_HEAVY_TEST_WAIT_S='0')
        for name in ('CI', 'AGENT_HUB_HEAVY_TEST_HELD', 'AGENT_HUB_HEAVY_TEST_HOLDER_TOKEN',
                     'AGENT_HUB_HEAVY_TEST_RUN_ID',
                     'AGENT_HUB_HEAVY_TEST_AUTO'):
            self.env.pop(name, None)
        self.bad_states = ['{invalid', json.dumps({'version': 99, 'next': 1, 'active': {}})]

    def tearDown(self):
        self.temp.cleanup()

    def test_wrapper_refuses_corrupt_and_unknown_state_without_running_command(self):
        marker = self.root / 'command-ran'
        for raw in self.bad_states:
            with self.subTest(state=raw):
                self.state_path.write_text(raw)
                result = subprocess.run([str(WRAPPER), '--', sys.executable, '-c',
                    f'from pathlib import Path; Path({str(marker)!r}).touch()'],
                    env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn('admission refused', result.stderr)
                self.assertFalse(marker.exists())
                self.assertEqual(self.state_path.read_text(), raw)
        # Usage errors must keep their original exit code even with bad state.
        result = subprocess.run([str(WRAPPER), '--slots', '0', '--', 'true'],
                                env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)

    def test_real_vitest_hook_refuses_corrupt_and_unknown_state(self):
        # Run the real exported hook, without starting Vitest/workerd or any
        # heavy workload. It must reject rather than return an uncapped run.
        code = """import setup from './test/heavy-slot.global-setup.mjs';
try { const cleanup = await setup(); if (cleanup) cleanup();
      console.log('UNEXPECTED_CONTINUE'); process.exitCode = 1; }
catch (error) { console.log('REFUSED:' + error.message); process.exitCode = 3; }
"""
        for raw in self.bad_states:
            with self.subTest(state=raw):
                self.state_path.write_text(raw)
                result = subprocess.run(['node', '--input-type=module', '-e', code],
                    cwd=WRAPPER.parent.parent, env=self.env, capture_output=True,
                    text=True, timeout=10)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn('REFUSED:heavy-test slot not acquired', result.stdout)
                self.assertNotIn('UNEXPECTED_CONTINUE', result.stdout)

    def test_real_pytest_hook_refuses_corrupt_and_unknown_state(self):
        # Compile the actual hook definitions, leaving unrelated autouse
        # fixtures/import-time profile isolation out of this stdlib unit test.
        # Only pytest's exception type is supplied; the holder is real.
        path = WRAPPER.parent.parent / 'host/tests/conftest.py'
        tree = ast.parse(path.read_text(), filename=str(path))
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name in ('heavy_slot_wanted', 'pytest_configure')]
        self.assertEqual(len(definitions), 2)
        class Refused(RuntimeError):
            pass
        namespace = {'__file__': str(path), 'os': os, 'Path': Path,
                     'pytest': SimpleNamespace(UsageError=Refused)}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), 'exec'), namespace)
        real_popen = subprocess.Popen
        holders = []
        def capture_holder(*args, **kwargs):
            holder = real_popen(*args, **kwargs)
            holders.append(holder)
            return holder
        try:
            for raw in self.bad_states:
                with self.subTest(state=raw):
                    self.state_path.write_text(raw)
                    with mock.patch.dict(os.environ, self.env, clear=True), \
                         mock.patch.object(subprocess, 'Popen', capture_holder):
                        with self.assertRaisesRegex(Refused, 'heavy-test slot not acquired'):
                            namespace['pytest_configure'](SimpleNamespace())
                        self.assertNotEqual(os.environ.get('AGENT_HUB_HEAVY_TEST_HELD'), '1')
        finally:
            for holder in holders:
                holder.wait(timeout=10)
                holder.stdin.close()
                holder.stdout.close()


class QueueCostTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('heavy_admission', WRAPPER.with_name('heavy-test-admission.py'))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_invalid_priority_row_is_coerced_not_fatal(self):
        root = Path(tempfile.mkdtemp(dir=WRAPPER.parent.parent / '.local/tmp'))
        (root / 'fifo-state.json').write_text(json.dumps({'version': 1, 'next': 5, 'active': {
            '0': {'sequence': 3, 'priority': 'intake', 'created': 1.0}}}))
        state = self.module.Queue(root).load()
        self.assertEqual(state['active']['0']['priority'], 'normal')

    def test_invalid_env_priority_default_is_coerced(self):
        root = Path(tempfile.mkdtemp(dir=WRAPPER.parent.parent / '.local/tmp'))
        env = dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=str(root), AGENT_HUB_HEAVY_TEST_PRIORITY='intake',
                   AGENT_HUB_HEAVY_TEST_SLOTS='1', AGENT_HUB_HEAVY_TEST_WAIT_S='5')
        env.pop('AGENT_HUB_HEAVY_TEST_HELD', None)
        result = subprocess.run([sys.executable, str(WRAPPER.with_name('heavy-test-admission.py')), '--', 'true'],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('invalid AGENT_HUB_HEAVY_TEST_PRIORITY', result.stderr)
        self.assertIn('admitted normal ticket', result.stderr)

    def test_run_id_keeps_queue_place_across_phases(self):
        queue = self.module.Queue(Path(tempfile.mkdtemp(dir=WRAPPER.parent.parent / '.local/tmp')))
        state = {'version': 1, 'next': 1, 'active': {}}
        with mock.patch.dict(os.environ, {self.module.RUN_ID_ENV: 'full-1'}):
            first, row = queue.new_ticket(state, 'final', 10.0)
        for other in range(5):
            state['active'][str(other)] = {'sequence': state['next'], 'priority': 'final', 'created': 20.0}
            state['next'] += 1
        with mock.patch.dict(os.environ, {self.module.RUN_ID_ENV: 'full-1'}):
            again, row = queue.new_ticket(state, 'final', 500.0)
        self.assertEqual(again, first)
        self.assertEqual(row['created'], 10.0)
        state['active']['9'] = row
        self.assertEqual(queue.head(state, 600.0), '9')
        # No run id, or an expired one, takes a fresh tail sequence.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.module.RUN_ID_ENV, None)
            fresh, _ = queue.new_ticket(state, 'final', 600.0)
        self.assertEqual(fresh, state['next'] - 1)
        with mock.patch.dict(os.environ, {self.module.RUN_ID_ENV: 'full-1'}):
            expired, _ = queue.new_ticket(state, 'final', 500.0 + self.module.RUN_AFFINITY_S + 1)
        self.assertGreater(expired, first)

    def test_run_id_garbage_and_bad_run_records_are_not_fatal(self):
        root = Path(tempfile.mkdtemp(dir=WRAPPER.parent.parent / '.local/tmp'))
        (root / 'fifo-state.json').write_text(json.dumps({'version': 1, 'next': 5, 'active': {},
                                                          'runs': {'a': {'sequence': 'x'}, 'b': 3}}))
        queue = self.module.Queue(root)
        self.assertEqual(queue.load()['runs'], {})
        with mock.patch.dict(os.environ, {self.module.RUN_ID_ENV: '../../bad id'}):
            self.assertIsNone(queue.run_id())

    def test_release_outranks_final_and_aged_normal(self):
        # Intake 1293b560 AC3: release is its own class above final; aging
        # promotes a normal ticket to the final class only, never to release.
        state = {'active': {
            '0': {'sequence': 1, 'priority': 'normal', 'created': 0},
            '1': {'sequence': 2, 'priority': 'final', 'created': 100},
            '2': {'sequence': 3, 'priority': 'release', 'created': 100}}}
        now = self.module.NORMAL_AGE_S + 1000
        self.assertEqual(self.module.Queue.head(state, now), '2')
        position, waiting = self.module.Queue.position(state, '2', now)
        self.assertEqual((position, waiting), (1, 3))

    def test_refused_retry_reclaims_place_and_age(self):
        # Intake 1293b560 AC1: a capacity-refused retry with the same run id
        # keeps its queue position (and age) instead of re-queueing at the
        # back; the older ticket is admitted before newer arrivals.
        queue = self.module.Queue(Path(tempfile.mkdtemp(dir=WRAPPER.parent.parent / '.local/tmp')))
        state = {'version': 1, 'next': 1, 'active': {}, 'runs': {}}
        with mock.patch.dict(os.environ, {self.module.RUN_ID_ENV: 'chain-1'}):
            first, row = queue.new_ticket(state, 'normal', 1000.0)
        state['active']['0'] = row
        state['next'] += 1
        state['active']['1'] = {'sequence': state['next'], 'priority': 'normal', 'created': 1100.0}
        state['next'] += 1
        state['active']['2'] = {'sequence': state['next'], 'priority': 'final', 'created': 1200.0}
        state['next'] += 1
        # Capacity refusal parks our row but keeps the run record.
        with mock.patch.dict(os.environ, {self.module.RUN_ID_ENV: 'chain-1'}):
            queue.park_refused(state, '0', 2000.0)
        self.assertNotIn('0', state['active'])
        # The retry reclaims the original sequence and creation time.
        with mock.patch.dict(os.environ, {self.module.RUN_ID_ENV: 'chain-1'}):
            again, again_row = queue.new_ticket(state, 'normal', 2500.0)
        self.assertEqual(again, first)
        self.assertEqual(again_row['created'], 1000.0)
        state['active']['3'] = again_row
        with mock.patch.object(self.module, 'NORMAL_AGE_S', 1500):
            self.assertEqual(queue.head(state, 2500.0), '3')
            position, _ = queue.position(state, '3', 2500.0)
        self.assertEqual(position, 1)

    def test_park_without_run_id_drops_dead_row(self):
        queue = self.module.Queue(Path(tempfile.mkdtemp(dir=WRAPPER.parent.parent / '.local/tmp')))
        state = {'version': 1, 'next': 3,
                 'active': {'0': {'sequence': 1, 'priority': 'normal', 'created': 5.0}},
                 'runs': {}}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.module.RUN_ID_ENV, None)
            queue.park_refused(state, '0', 10.0)
        self.assertEqual(state['active'], {})
        self.assertEqual(state.get('runs'), {})

    def test_aged_normal_precedes_continuous_new_finals(self):
        state = {'active': {'0': {'sequence': 1, 'priority': 'normal', 'created': 0}}}
        for i in range(2, 1002):
            state['active'][str(i)] = {'sequence': i, 'priority': 'final', 'created': i / 10}
        self.assertNotEqual(self.module.Queue.head(state, self.module.NORMAL_AGE_S - 1), '0')
        self.assertEqual(self.module.Queue.head(state, self.module.NORMAL_AGE_S), '0')

    def test_repeated_runs_reuse_lease_inode_and_keep_counter(self):
        # Thousands of admissions with real flock/atomic state writes but no
        # child workloads; directory count stays at peak concurrency, one.
        parent = WRAPPER.parent.parent / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            queue = self.module.Queue(Path(temp))
            first_inode = None
            for i in range(1000):
                state = queue.load()
                self.assertEqual(state['next'], i + 1)
                with queue.open_lease('0') as lease:
                    self.assertTrue(self.module.lock_available(lease))
                    inode = os.fstat(lease.fileno()).st_ino
                    first_inode = inode if first_inode is None else first_inode
                    self.assertEqual(inode, first_inode)
                    state['active']['0'] = {'sequence': state['next'], 'priority': 'normal', 'created': time.monotonic()}
                    state['next'] += 1
                    queue.save(state)
                # An unlocked recorded lease models a crashed waiter.
                queue.prune_dead(state)
                self.assertEqual(state['active'], {})
                queue.save(state)
            self.assertEqual(len(list(queue.leases.iterdir())), 1)
            self.assertEqual(queue.load()['next'], 1001)

    def test_wait_cost_bounded_with_many_waiters(self):
        # Exercise the real scheduler with 40 active tickets, a very large
        # historical counter, and unavailable slots, without spawning load.
        # Every non-head retry opens only the head's lease and no slot files;
        # jitter waits are >=1s (apart from the final deadline remainder).
        parent = WRAPPER.parent.parent / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            root = Path(temp)
            queue = self.module.Queue(root)
            state = {'version': 1, 'next': 1000001, 'active': {}}
            leases = []
            # pytest may own FD9 (capture or its heavy-slot holder pipe).
            # Save its open file description before exercising main in-process.
            try:
                original_fd9 = os.dup(9)
            except OSError as error:
                if error.errno != errno.EBADF:  # FD9 was originally closed
                    raise
                original_fd9 = None
            original_stat = os.fstat(original_fd9) if original_fd9 is not None else None
            original_inheritable = os.get_inheritable(9) if original_fd9 is not None else None
            reserved = os.open(os.devnull, os.O_RDONLY)
            os.dup2(reserved, 9)
            if reserved != 9:
                os.close(reserved)
            try:
                for i in range(40):
                    handle = queue.open_lease(str(i))
                    self.module.lock_available(handle)
                    leases.append(handle)
                    state['active'][str(i)] = {'sequence': 999900 + i, 'priority': 'normal', 'created': 1000}
                queue.save(state)
                opened = []
                original_open = Path.open
                def tracking_open(path, *args, **kwargs):
                    opened.append(path)
                    return original_open(path, *args, **kwargs)
                now = [1000.0]
                sleeps = []
                def sleep(delay):
                    sleeps.append(delay)
                    now[0] += delay
                diagnostics = io.StringIO()
                with mock.patch.dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=temp,
                                     AGENT_HUB_HEAVY_TEST_PRIORITY='final'), \
                     mock.patch.object(sys, 'argv', ['wrapper', '--priority', 'normal', '--slots', '1', '--wait', '5', '--', 'true']), \
                     mock.patch.object(sys, 'stderr', diagnostics), \
                     mock.patch.object(Path, 'open', tracking_open), \
                     mock.patch.object(self.module.signal, 'signal'), \
                     mock.patch.object(self.module.time, 'monotonic', lambda: now[0]), \
                     mock.patch.object(self.module.time, 'sleep', sleep), \
                     mock.patch.object(self.module.random, 'uniform', return_value=1.5):
                    self.assertEqual(self.module.main(), 3)
                self.assertIn('(normal ticket 1000001)', diagnostics.getvalue())
                self.assertIn('queue position 41/41', diagnostics.getvalue())
                self.assertNotIn('admitted', diagnostics.getvalue())
                # Registration probes 40 active leases once; four retries plus
                # the deadline probe touch just the head, not all 40 tickets.
                self.assertLessEqual(sum(p.parent == queue.leases for p in opened), 46)
                self.assertFalse(any(p.name.startswith('slot-') for p in opened))
                self.assertEqual(sleeps, [1.5, 1.5, 1.5, .5])
                self.assertEqual(len(list(queue.leases.iterdir())), 41)
            finally:
                for lease in leases:
                    lease.close()
                # Restore an ambient harness descriptor, including the same
                # open file description/pipe, rather than merely closing FD9.
                if original_fd9 is not None:
                    os.dup2(original_fd9, 9, inheritable=original_inheritable)
                    os.close(original_fd9)
                else:
                    try:
                        os.close(9)
                    except OSError:
                        pass
            # Keep assertions outside finally so they cannot mask a failure
            # in the cost check itself.
            if original_stat is not None:
                restored = os.fstat(9)
                self.assertEqual((restored.st_dev, restored.st_ino),
                                 (original_stat.st_dev, original_stat.st_ino))
                self.assertEqual(os.get_inheritable(9), original_inheritable)


if __name__ == '__main__':
    unittest.main()
