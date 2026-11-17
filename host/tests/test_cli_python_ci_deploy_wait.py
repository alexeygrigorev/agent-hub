"""Synthetic GitHub metadata/fake-clock only; no network, workflow or deploy."""
import importlib.util
from pathlib import Path
import unittest
import subprocess
import sys
import time
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[2] / 'scripts/wait-python-host-tests.py'
spec = importlib.util.spec_from_file_location('python_ci_wait', MODULE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = 'a' * 40
REPO = 'fixture/repository'


def run(status='in_progress', conclusion=None, **extra):
    return dict(head_sha=SHA, head_branch='main', event='push', status=status, conclusion=conclusion, **extra)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PythonDeployWaitTests(unittest.TestCase):
    def wait(self, fetch, clock):
        gate.wait_for_python(REPO,SHA,fetch=fetch,monotonic=clock.monotonic,sleep=clock.sleep)

    def test_actual_retained_green_after_old_ten_minute_gate_is_waited(self):
        clock = Clock()
        calls = []
        def fetch(repo,sha,timeout):
            calls.append((repo,sha,timeout))
            return {'workflow_runs':[run('completed','success') if clock.now >= 799 else run()]}
        self.wait(fetch,clock)
        self.assertEqual(clock.now,800)
        self.assertGreater(clock.now,60*10)
        self.assertTrue(all(repo==REPO and sha==SHA and 0<timeout<=20 for repo,sha,timeout in calls))

    def test_twenty_minute_execution_plus_bounded_queue_remains_eligible(self):
        clock = Clock()
        def fetch(*args):
            if clock.now < 240:return {'workflow_runs':[run('queued')]}
            return {'workflow_runs':[run('completed','success') if clock.now >= 1440 else run()]}
        self.wait(fetch,clock)
        self.assertEqual(clock.now,1440)

    def test_no_matching_run_expires_once_with_clipped_sleeps(self):
        clock=Clock();calls=[]
        def fetch(repo,sha,timeout):
            calls.append(timeout)
            return {'workflow_runs':[]}
        with self.assertRaisesRegex(gate.WaitRefused,'python_ci_wait_deadline'):self.wait(fetch,clock)
        self.assertEqual(clock.now,1500)
        self.assertEqual(len(calls),150)
        self.assertTrue(all(0 < delay <= 10 for delay in clock.sleeps))
        self.assertEqual(calls[-1],10)

    def test_terminal_failures_fail_fast_without_sleep_or_retry(self):
        for conclusion in ['failure','cancelled','timed_out','action_required','neutral','skipped',None]:
            with self.subTest(conclusion=conclusion):
                clock=Clock();calls=[]
                def fetch(*args):
                    calls.append(True)
                    return {'workflow_runs':[run('completed',conclusion)]}
                with self.assertRaisesRegex(gate.WaitRefused,'python_ci_terminal_failure'):self.wait(fetch,clock)
                self.assertEqual(calls,[True]);self.assertEqual(clock.sleeps,[])

    def test_foreign_sha_branch_or_event_success_never_authorizes_target(self):
        for field,value in [('head_sha','b'*40),('head_branch','other'),('event','pull_request')]:
            with self.subTest(field=field):
                wrong=run('completed','success');wrong[field]=value
                self.assertEqual(gate.decision({'workflow_runs':[wrong]},SHA),'pending')

    def test_one_host_job_or_in_progress_workflow_is_not_green(self):
        self.assertEqual(gate.decision({'workflow_runs':[run()]},SHA),'pending')
        self.assertEqual(gate.decision({'workflow_runs':[run('queued')]},SHA),'pending')

    def test_late_success_from_blocking_fetch_is_rejected(self):
        clock=Clock()
        def fetch(*args):
            clock.now=1500
            return {'workflow_runs':[run('completed','success')]}
        with self.assertRaisesRegex(gate.WaitRefused,'python_ci_wait_deadline'):self.wait(fetch,clock)
        self.assertEqual(clock.sleeps,[])

    def test_fetch_and_sleep_time_use_original_deadline_not_attempt_renewal(self):
        clock=Clock();calls=[]
        def fetch(repo,sha,timeout):
            calls.append(timeout);clock.now += timeout
            return {'workflow_runs':[run()]}
        with self.assertRaisesRegex(gate.WaitRefused,'python_ci_wait_deadline'):self.wait(fetch,clock)
        self.assertEqual(clock.now,1500)
        self.assertEqual(sum(calls)+sum(clock.sleeps),1500)
        self.assertLessEqual(calls[-1],20)

    def test_metadata_failure_is_not_turned_into_green_or_retried(self):
        clock=Clock()
        def fetch(*args):raise gate.WaitRefused('python_ci_metadata_unavailable')
        with self.assertRaisesRegex(gate.WaitRefused,'python_ci_metadata_unavailable'):self.wait(fetch,clock)
        self.assertEqual(clock.sleeps,[])
        with self.assertRaisesRegex(gate.WaitRefused,'python_ci_metadata_invalid'):gate.decision({},SHA)

    def test_same_sha_success_selection_policy_is_preserved(self):
        self.assertEqual(gate.decision({'workflow_runs':[run('completed','success'),run()]},SHA),'green')
        self.assertEqual(gate.decision({'workflow_runs':[run('completed','failure'),run()]},SHA),'pending')

    def test_invalid_selection_refuses_before_fetch(self):
        calls=[]
        with self.assertRaisesRegex(gate.WaitRefused,'python_ci_selection_invalid'):
            gate.wait_for_python(REPO,'not-a-sha',fetch=lambda *args:calls.append(args))
        self.assertEqual(calls,[])


class PythonDeployTransportTests(unittest.TestCase):
    def invoke(self, source, timeout=2):
        original = subprocess.Popen
        self.children = []
        def spawn(argv, **kwargs):
            self.assertEqual(argv[:2], ['gh', 'api'])
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            child = original([sys.executable, '-I', '-S', '-c', source], **kwargs)
            self.children.append(child)
            return child
        try:
            with patch.object(gate.subprocess, 'Popen', side_effect=spawn):
                return gate.fetch_runs(REPO, SHA, timeout)
        finally:
            for child in self.children:
                self.assertIsNotNone(child.poll(), 'owned direct child must settle')
                self.assertTrue(child.stdout.closed)

    def test_actual_transport_parses_small_response(self):
        self.assertEqual(self.invoke('print(\'{"workflow_runs": []}\')'), {'workflow_runs': []})

    def test_stdout_flood_refuses_before_child_completion(self):
        started = time.monotonic()
        with self.assertRaisesRegex(gate.WaitRefused, '^python_ci_metadata_unavailable$'):
            self.invoke('import os,time;os.write(1,b"x"*2097152);time.sleep(10)')
        self.assertLess(time.monotonic()-started, 3)

    def test_stderr_flood_discarded_without_deadlock_or_capture(self):
        self.assertEqual(self.invoke('import os;os.write(2,b"x"*4194304);print(\'{"workflow_runs": []}\')'), {'workflow_runs': []})

    def test_timeout_terminates_and_reaps_direct_child(self):
        with self.assertRaisesRegex(gate.WaitRefused, '^python_ci_metadata_unavailable$'):
            self.invoke('import time;time.sleep(10)', timeout=0.2)
        self.assertNotEqual(self.children[0].returncode, 0)

    def test_timeout_kills_term_resistant_direct_child(self):
        started = time.monotonic()
        with self.assertRaisesRegex(gate.WaitRefused, '^python_ci_metadata_unavailable$'):
            self.invoke('import signal,time,os;signal.signal(signal.SIGTERM,signal.SIG_IGN);os.write(1,b" ");time.sleep(10)', timeout=0.3)
        self.assertEqual(self.children[0].returncode, -9)
        self.assertLess(time.monotonic()-started, 2)

    def test_small_deep_json_is_fixed_refusal_not_traceback(self):
        with self.assertRaisesRegex(gate.WaitRefused, '^python_ci_metadata_unavailable$'):
            self.invoke('print("["*200000+"0"+"]"*200000)')


if __name__=='__main__':
    unittest.main()
