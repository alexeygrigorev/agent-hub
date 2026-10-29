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


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        parent = WRAPPER.parent.parent / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.children = []
        self.env = dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=str(self.root),
                        AGENT_HUB_HEAVY_TEST_SLOTS='1', AGENT_HUB_HEAVY_TEST_WAIT_S='40',
                        AGENT_HUB_HEAVY_TEST_PRIORITY='normal')
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

    def until(self, condition):
        deadline = time.monotonic() + 30
        while not condition():
            if time.monotonic() > deadline:
                self.fail('timed out waiting for test event')
            time.sleep(.02)

    def start(self, name, priority='normal', code=None, wait='40'):
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
        child = subprocess.Popen(command + ['--priority', priority, '--wait', wait,
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

    def test_aged_waiter_cannot_be_overtaken_by_new_finals(self):
        self.scheduler_clock_origin = time.monotonic()
        normal = self.start('normal')
        first_final = self.start('final1', 'final')
        # Advance only this fixture's normal registration age, under the same
        # mutex used by the real scheduler, rather than waiting five minutes.
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

    def test_timeout_and_exit_status(self):
        timed = self.start('timeout', wait='0')
        self.assertEqual(timed.wait(timeout=40), 3)
        self.holder.close()
        child = self.start('exit', code='raise SystemExit(17)')
        self.assertEqual(child.wait(timeout=40), 17)

    def test_term_retains_slot_until_command_finishes(self):
        self.holder.close()
        marker = self.root / 'started'
        release = self.root / 'release'
        code = f"from pathlib import Path; import time; Path({str(marker)!r}).touch()\nwhile not Path({str(release)!r}).exists(): time.sleep(.02)"
        holder = self.start('holder', code=code)
        self.until(marker.exists)
        holder.terminate()
        waiter = self.start('next')
        time.sleep(.2)
        self.assertFalse((self.root / 'order').exists())
        release.touch()
        self.assertEqual(holder.wait(timeout=40), 143)
        self.assertEqual(waiter.wait(timeout=40), 0)


class RefusalTests(unittest.TestCase):
    def setUp(self):
        parent = WRAPPER.parent.parent / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temp.name)
        self.state_path = self.root / 'fifo-state.json'
        self.env = dict(os.environ, AGENT_HUB_HEAVY_TEST_DIR=str(self.root),
                        AGENT_HUB_HEAVY_TEST_SLOTS='1', AGENT_HUB_HEAVY_TEST_WAIT_S='0')
        for name in ('CI', 'AGENT_HUB_HEAVY_TEST_HELD', 'AGENT_HUB_HEAVY_TEST_AUTO'):
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

    def test_aged_normal_precedes_continuous_new_finals(self):
        state = {'active': {'0': {'sequence': 1, 'priority': 'normal', 'created': 0}}}
        for i in range(2, 1002):
            state['active'][str(i)] = {'sequence': i, 'priority': 'final', 'created': i / 10}
        self.assertNotEqual(self.module.Queue.head(state, 299), '0')
        self.assertEqual(self.module.Queue.head(state, 300), '0')

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
