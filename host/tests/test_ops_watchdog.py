"""Exercise the real ops shell controller with isolated runtime/OS adapters."""
import json
import re
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

CTL = Path(__file__).resolve().parents[2] / 'ops/lib/session-ctl.sh'
ADAPTER = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
p=pathlib.Path(os.environ['HOME'])
f=p/'runtime.json'
s=json.loads(f.read_text())
with (p/'calls').open('a') as out: out.write(name+' '+ ' '.join(args)+'\n')
def save(): f.write_text(json.dumps(s))
if name=='a':
    cmd=args[0]
    if cmd=='list':
        if s.get('census_error'): sys.exit(1)
        print(json.dumps(s['sessions']))
    elif cmd=='status': print(json.dumps(next(x for x in s['sessions'] if x['id']==args[1])))
    elif cmd=='kill':
        if s.get('kill_ok'):
            s['sessions']=[x for x in s['sessions'] if x['id']!=args[1]]; save()
        else: sys.exit(1)
    elif cmd=='forget':
        s['sessions']=[x for x in s['sessions'] if x['id']!=args[1]];save()
    elif cmd=='start':
        if s.get('start_error'): sys.exit(1)
        s['sessions'].append(dict(id='new',tag='orchestrator-main',workspace=str(p/'git/agent-hub-wt-orchestrator-run/ops/orchestrator'),phase='running',worker_alive=True,worker_pid=os.getppid(),workload_pid=os.getppid()))
        save()
    elif cmd=='capture': print('❯')
    elif cmd=='send': sys.exit(s.get('send_error',0))
elif name=='systemd-run':
    import subprocess
    # Proof timers (--on-active) are only recorded; scope launches run their command.
    sys.exit(subprocess.call(args[args.index('--')+1:]) if '--' in args else 0)
elif name=='hub':
    sys.exit(s.get('hub_error',0))
elif name=='git':
    if 'rev-parse' in args:
        print(str(p/'exclude') if '--git-path' in args else 'abcdef')
'''


class OpsWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        for cmd in ('a', 'git', 'gh', 'sleep', 'systemd-run', 'systemctl', 'hub'):
            file = self.bin / cmd
            file.write_text(ADAPTER)
            file.chmod(0o755)
        (self.home / 'git/bot-skills-ops/.git').mkdir(parents=True)
        self.workspace = self.home / 'git/agent-hub-wt-orchestrator-run/ops/orchestrator'
        self.workspace.mkdir(parents=True)
        (self.workspace.parents[1] / '.git').touch()
        (self.home / 'exclude').touch()
        self.runtime = self.home / 'runtime.json'
        self.state = self.home / 'tmp/agent-reports/orchestrator/state'
        self.ticks = self.home / 'tmp/agent-reports/orchestrator-ticks.jsonl'
        self.ticks.parent.mkdir(parents=True)
        self.ticks.write_text('{}\n')  # fresh tick log: the orchestrator is progressing
        self.data = {'sessions': []}
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin)+':'+os.environ['PATH'], 'LOOP_INTERVAL': 'off', 'MIN_RESTART_GAP': '0', 'OPS_LOCK_DIR': str(self.home)}

    def session(self, alive=False, pid=2147483647):
        return dict(id='old', tag='orchestrator-main', workspace=str(self.workspace), phase='running', worker_alive=alive, worker_pid=pid, workload_pid=pid)

    def run_ctl(self, command='check'):
        self.runtime.write_text(json.dumps(self.data))
        result = subprocess.run(['bash', str(CTL), 'orchestrator', command], env=self.env, text=True, capture_output=True, timeout=15)
        self.data = json.loads(self.runtime.read_text())
        return result

    def calls(self):
        f = self.home / 'calls'
        return f.read_text() if f.exists() else ''

    def test_live_worker_preserved_without_checkout(self):
        self.data['sessions'] = [self.session(True)]
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already running: old', result.stdout)
        self.assertNotIn('git ', self.calls())
        self.assertNotIn('a kill', self.calls())

    def test_dead_record_forget_and_real_start(self):
        self.data['sessions'] = [self.session()]
        result = self.run_ctl()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('a forget old --force', self.calls())
        self.assertIn('recovery path 1 started', result.stdout)
        self.assertTrue((self.state / 'proof_due').exists())
        self.assertNotIn('already running', result.stdout)

    def test_surviving_workload_blocks_forget_and_launch(self):
        self.data['sessions'] = [self.session(pid=os.getpid())]
        result = self.run_ctl()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('a forget', self.calls())
        self.assertNotIn('a start', self.calls())

    def test_unknown_pid_blocks_forget(self):
        session = self.session()
        del session['workload_pid']
        self.data['sessions'] = [session]
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertNotIn('a forget', self.calls())

    def test_two_failures_alert_both_durable_destinations(self):
        self.data['start_error'] = True
        self.assertNotEqual(self.run_ctl().returncode, 0)
        needs = self.home / 'tmp/agent-reports/orchestrator/NEEDS_ALEXEY.md'
        self.assertFalse(needs.exists())
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertIn('automatic recovery failed 2 consecutive', needs.read_text())
        inbox = self.home / 'tmp/agent-reports/oncall/inbox'
        self.assertEqual(len(list(inbox.glob('watchdog-*.md'))), 1)

    def test_recovery_resets_consecutive_failures_only_after_proof(self):
        self.data['start_error'] = True
        self.run_ctl()
        self.data['start_error'] = False
        self.assertEqual(self.run_ctl().returncode, 0)
        # A started process is not proof of recovery.
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '1')
        self.tick(after_start=5)
        result = self.run_ctl()
        self.assertIn('recovery proven', result.stdout)
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '0')

    def test_census_error_never_starts_duplicate(self):
        self.data['census_error'] = True
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertNotIn('a start', self.calls())

    def test_stop_failure_propagates_manual_restart(self):
        self.data['sessions'] = [self.session(True, os.getpid())]
        self.assertNotEqual(self.run_ctl('restart').returncode, 0)
        self.assertNotIn('a start', self.calls())

    def test_live_session_after_old_dead_record_selected(self):
        self.data['sessions'] = [self.session(), {**self.session(True), 'id': 'live'}]
        result = self.run_ctl()
        self.assertEqual(result.returncode, 0)
        self.assertIn('OK session=live', result.stdout)
        self.assertNotIn('a kill', self.calls())

    def test_stale_tick_log_is_stall(self):
        self.data['sessions'] = [{**self.session(True), 'id': 'live'}]
        os.utime(self.ticks, (0, 0))
        result = self.run_ctl()
        self.assertIn('STALLED session=live', result.stdout)

    def test_failed_loop_send_is_restart_failure(self):
        self.env['LOOP_INTERVAL'] = '1h'
        self.data['send_error'] = 1
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '1')


    # Intake 500fd60b AC2: bounded idle, proven restarts and a second recovery path.
    def tick(self, after_start=0, age=None):
        start = int((self.state / 'last_start').read_text()) if (self.state / 'last_start').exists() else 0
        when = start + after_start if age is None else time.time() - age
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when))
        with self.ticks.open('a') as f: f.write(json.dumps({'ts': stamp}) + '\n')
        os.utime(self.ticks, (when, when))

    def due_now_keep_window(self):
        # Move the whole recovery 1200s into the past: start, due and every tick shift together.
        import calendar
        start = int((self.state / 'last_start').read_text()) - 1200
        (self.state / 'last_start').write_text(str(start))
        (self.state / 'proof_due').write_text(f'{start + 600} 1\n')
        shifted = []
        for line in self.ticks.read_text().splitlines():
            entry = json.loads(line)
            if 'ts' not in entry: continue
            t = calendar.timegm(time.strptime(entry['ts'], '%Y-%m-%dT%H:%M:%SZ')) - 1200
            shifted.append(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))}) + '\n')
        self.ticks.write_text(''.join(shifted))

    def due_now(self):
        due = self.state / 'proof_due'
        _, path = due.read_text().split()
        due.write_text(f'{int(time.time()) - 1} {path}\n')

    def timer_delays(self, kind):
        out = []
        for line in self.calls().splitlines():
            if line.startswith('systemd-run') and f'--unit ops-{kind}-orchestrator-' in line:
                self.assertIn('--timer-property=AccuracySec=1s', line)
                # runs this role's check directly: never queued behind watchdog.sh/oncall/the service lock
                self.assertIn('/usr/bin/timeout 900 ' + str(CTL) + ' orchestrator check', line)
                self.assertNotIn('watchdog.sh', line)
                out.append(int(re.search(r'--on-active=(\d+)', line).group(1)))
        return out

    def test_tick_only_after_proof_deadline_fails_and_starts_path_two(self):
        self.data['sessions'] = [self.session()]
        self.data['kill_ok'] = True
        self.run_ctl()
        self.tick(after_start=700)  # first tick 100s after the 600s window
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 1)', result.stdout)
        self.assertIn('recovery path 2 started', result.stdout)
        self.assertIn('--engine sonnet', self.starts()[-1])
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '1')

    def test_on_time_tick_in_history_proves_even_after_a_later_tick(self):
        self.data['sessions'] = [self.session()]
        self.run_ctl()
        self.tick(after_start=120)
        self.tick(after_start=700)  # newest mtime is late; the history has the on-time tick
        self.due_now_keep_window()
        result = self.run_ctl()
        self.assertIn('recovery proven: fresh tick 120s after start (path 1)', result.stdout)
        self.assertEqual(len(self.starts()), 1)

    def test_role_lock_serializes_orchestrator_checks(self):
        import fcntl
        self.data['sessions'] = [self.session()]
        self.env['ROLE_LOCK_WAIT'] = '1'
        with open(self.home / 'ops-ctl-orchestrator.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_ctl()
        self.assertEqual(result.returncode, 75)
        self.assertIn('role lock busy', result.stdout)
        self.assertEqual(self.starts(), [])

    def test_deadline_timer_bounds_detection_to_thirty_minutes(self):
        self.data['sessions'] = [{**self.session(True, os.getpid()), 'id': 'live'}]
        self.data['kill_ok'] = True
        self.tick(age=29 * 60)
        self.assertIn('OK session=live', self.run_ctl().stdout)
        delays = self.timer_delays('deadline')
        self.assertTrue(55 <= delays[-1] <= 60, delays)  # re-check exactly when the tick turns 30 min old
        self.assertIn("systemctl --user stop ops-deadline-orchestrator-*.timer", self.calls())  # one pending deadline
        self.tick(age=30 * 60)  # exactly 1800s is a stall
        self.assertIn('STALLED session=live: no tick for 1800s', self.run_ctl().stdout)

    def starts(self):
        return [line for line in self.calls().splitlines() if line.startswith('a start')]

    def test_29_oct_live_session_without_ticks_is_restarted_not_ok(self):
        self.data['sessions'] = [{**self.session(True, os.getpid()), 'id': 'live'}]
        self.data['kill_ok'] = True
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'last_start').write_text(str(int(time.time()) - 33091))
        self.ticks.unlink()  # the pre-266329ee prompt never wrote the tick log
        result = self.run_ctl()
        self.assertIn('STALLED session=live: no tick for 3309', result.stdout)
        self.assertNotIn('OK session=live', result.stdout)
        self.assertIn('recovery path 1 started', result.stdout)
        self.assertIn('hub ops heartbeat --role orchestrator --kind watchdog --state recovering', self.calls())

    def test_idle_bound_is_thirty_minutes(self):
        self.data['sessions'] = [{**self.session(True, os.getpid()), 'id': 'live'}]
        self.data['kill_ok'] = True
        self.tick(age=29 * 60)
        self.assertIn('OK session=live', self.run_ctl().stdout)
        self.assertIn('--state ok', self.calls())
        self.tick(age=31 * 60)
        self.assertIn('STALLED session=live', self.run_ctl().stdout)

    def test_restart_proven_by_fresh_tick_within_window(self):
        self.data['sessions'] = [self.session()]
        self.run_ctl()
        # exact-time proof check: due = recorded start + 600s, 1s timer accuracy
        delays = self.timer_delays('proof')
        self.assertEqual(len(delays), 1)
        self.assertTrue(590 <= delays[0] <= 601, delays)
        result = self.run_ctl()
        self.assertIn('recovery pending proof: session=new', result.stdout)
        self.assertEqual(len(self.starts()), 1)  # no second restart inside the window
        self.tick(after_start=120)
        result = self.run_ctl()
        self.assertIn('recovery proven: fresh tick 120s after start (path 1)', result.stdout)
        self.assertFalse((self.state / 'proof_due').exists())
        self.assertIn('OK session=new', result.stdout)

    def test_unproven_restart_starts_second_path_then_alerts_after_two(self):
        self.data['sessions'] = [self.session()]
        self.data['kill_ok'] = True
        self.run_ctl()
        self.assertIn('--engine opus', self.starts()[-1])
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 1)', result.stdout)
        self.assertIn('recovery path 2 started', result.stdout)
        self.assertIn('--engine sonnet', self.starts()[-1])
        needs = self.home / 'tmp/agent-reports/orchestrator/NEEDS_ALEXEY.md'
        self.assertFalse(needs.exists())  # one failed recovery is not an alert
        self.assertIn('--state proof_failed --recovery-failures 1', self.calls())
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 2)', result.stdout)
        self.assertIn('automatic recovery failed 2 consecutive times', needs.read_text())
        self.assertIn('--recovery-failures 2', self.calls())
        self.assertIn('--engine opus', self.starts()[-1])  # keeps recovering after the alert
        self.assertEqual(len(self.starts()), 3)

    def test_session_dying_inside_proof_window_fails_proof_immediately(self):
        self.data['sessions'] = [self.session()]
        self.run_ctl()
        self.data['sessions'] = [{**self.data['sessions'][0], 'worker_alive': False}]
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 1)', result.stdout)

    def test_hub_report_failure_never_blocks_recovery(self):
        self.data['hub_error'] = 1
        self.data['sessions'] = [self.session()]
        result = self.run_ctl()
        self.assertIn('hub ops report failed', result.stdout)
        self.assertIn('recovery path 1 started', result.stdout)


if __name__ == '__main__':
    unittest.main()
