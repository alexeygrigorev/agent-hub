"""Exercise the real ops shell controller with isolated runtime/OS adapters."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
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
    sys.exit(subprocess.call(args[args.index('--')+1:]))
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
        for cmd in ('a', 'git', 'gh', 'sleep', 'systemd-run'):
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
        self.data = {'sessions': []}
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin)+':'+os.environ['PATH'], 'LOOP_INTERVAL': 'off', 'MIN_RESTART_GAP': '0'}

    def session(self, alive=False, pid=2147483647):
        return dict(id='old', tag='orchestrator-main', workspace=str(self.workspace), phase='running', worker_alive=alive, worker_pid=pid, workload_pid=pid)

    def run_ctl(self, command='check'):
        self.runtime.write_text(json.dumps(self.data))
        result = subprocess.run(['bash', str(CTL), 'orchestrator', command], env=self.env, text=True, capture_output=True, timeout=15)
        self.data = json.loads(self.runtime.read_text())
        return result

    def calls(self):
        return (self.home / 'calls').read_text()

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
        self.assertIn('recovery verified session=new', result.stdout)
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
        self.assertIn('restart failed 2 consecutive', needs.read_text())
        inbox = self.home / 'tmp/agent-reports/oncall/inbox'
        self.assertEqual(len(list(inbox.glob('watchdog-*.md'))), 1)

    def test_recovery_resets_consecutive_failures(self):
        self.data['start_error'] = True
        self.run_ctl()
        self.data['start_error'] = False
        self.assertEqual(self.run_ctl().returncode, 0)
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

    def test_failed_loop_send_is_restart_failure(self):
        self.env['LOOP_INTERVAL'] = '1h'
        self.data['send_error'] = 1
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '1')


if __name__ == '__main__':
    unittest.main()
