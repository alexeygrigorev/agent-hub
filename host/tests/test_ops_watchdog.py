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
HOST_SRC = Path(__file__).resolve().parents[1]
ADAPTER = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
p=pathlib.Path(os.environ['HOME'])
f=p/'runtime.json'
s=json.loads(f.read_text())
with (p/'calls').open('a') as out: out.write(name+' '+ ' '.join(args)+'\n')
def save(): f.write_text(json.dumps(s))
def fail(msg, code=1):
    sys.stderr.write(msg+'\n'); save(); sys.exit(code)
if name=='a':
    cmd=args[0]
    if cmd=='list':
        if s.get('census_error'): sys.exit(1)
        print(json.dumps(s['sessions']))
    elif cmd=='engines':
        print(json.dumps(s.get('engines', [{'name': 'antigravity'}, {'name': 'grok'}, {'name': 'opencode'}, {'name': 'zcodex'}])))
    elif cmd=='status': print(json.dumps(next(x for x in s['sessions'] if x['id']==args[1])))
    elif cmd=='kill':
        if s.get('kill_ok'):
            s['sessions']=[x for x in s['sessions'] if x['id']!=args[1]]; save()
        else: sys.exit(1)
    elif cmd=='forget':
        s['sessions']=[x for x in s['sessions'] if x['id']!=args[1]];save()
    elif cmd=='start': sys.exit(90)  # Coordinators never start natively (phase 6.1).
    elif cmd=='capture': print(s.get('capture_prompt', '❯'))
    elif cmd=='send':
        if s.get('capture_prompt'):
            s['capture_prompt']='❯'; save()
        sys.exit(s.get('send_error',0))
elif name=='systemd-run':
    import subprocess
    # Proof timers (--on-active) are only recorded; scope launches run their command.
    sys.exit(subprocess.call(args[args.index('--')+1:]) if '--' in args else 0)
elif name=='hub':
    if args and args[0]=='sessions':
        # The maintained CLI parser is the contract: unsupported flags such as
        # --tag or --json exit 2 here exactly as they do in production.
        import argparse
        sys.path.insert(0, os.environ['HUB_PARSER_ROOT'])
        from agent_hub_host import session_control
        parser=argparse.ArgumentParser(prog='hub'); session_control.add_commands(parser.add_subparsers(dest='cmd'))
        ns=parser.parse_args(args)
        hub=s.setdefault('hub',{})
        if ns.sessions_cmd=='start':
            assert ns.interactive and ns.wait and ns.purpose=='implement' and ns.key and ns.intake_task_id, args
            n=len(hub)+1; hid='hub-%04d' % n
            mode=s.get('start_error')
            if mode=='transport':
                fail('Session transport failed: connection or TLS failure (reconcile with --key %s via show/commands)' % ns.key)
            if mode=='http':
                fail('Session HTTP request failed with status 409 (no_host_capacity) (reconcile with --key %s via show/commands)' % ns.key)
            hub[hid]=dict(id=hid,state='refused' if mode else 'running',key=ns.key,name=ns.name)
            if mode:
                fail('session_start_failed; session %s reached refused (no_capacity); reconcile with --key %s via show/events' % (hid, ns.key))
            native='new' if n==1 else 'new%d' % n
            ws=s.get('resolved_workspace') or ns.workspace
            hub[hid]['native_id']=native
            s['sessions'].append(dict(id=native,tag='task-hub2-'+hid,workspace=ws,phase='running',worker_alive=True,
                worker_pid=os.getppid(),workload_pid=os.getppid(),labels={'hub.session':hid,'hub.managed':'true'}))
            bound=dict(id=hid,state='running',native_id=native,workspace=ws,requested_workspace=ns.workspace)
            save()
            print(json.dumps({**bound,'client_key':ns.key,'binding_wait':{'session':bound}}, indent=2))
        elif ns.sessions_cmd=='stop':
            row=hub.get(ns.id)
            if s.get('stop_error') or not row or row['state']!='running':
                fail('stop_not_running; command c1; stop was not confirmed')
            row['state']='stopped'
            s['sessions']=[x for x in s['sessions'] if x['id']!=row.get('native_id')]
            save()
            print(json.dumps({'state':'acked'}))
        elif ns.sessions_cmd=='show':
            row=hub.get(ns.id)
            if not row: fail('Session HTTP request failed with status 404 (not_found)')
            print(json.dumps({'session':row}))
        else:
            sys.exit(2)
        sys.exit(0)
    if args and args[0]=='quota':
        default={'pools': [
            {'provider': 'gemini', 'harness': ['antigravity'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
            {'provider': 'go', 'harness': ['opencode'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
            {'provider': 'grok', 'harness': ['grok'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
            {'provider': 'zai', 'harness': ['zcodex'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
        ]}
        print(json.dumps(s.get('quota',default)))
        sys.exit(s.get('quota_error',0))
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
        (self.home / 'git/bots-management-ops/.git').mkdir(parents=True)
        self.workspace = self.home / 'git/agent-hub-wt-orchestrator-run/ops/orchestrator'
        self.workspace.mkdir(parents=True)
        (self.workspace.parents[1] / '.git').touch()
        (self.home / 'exclude').touch()
        self.runtime = self.home / 'runtime.json'
        self.state = self.home / 'tmp/agent-reports/orchestrator/state'
        self.ticks = self.home / 'tmp/agent-reports/orchestrator-ticks.jsonl'
        self.ticks.parent.mkdir(parents=True)
        self.ticks.write_text('{}\n')  # fresh tick log: the orchestrator is progressing
        self.data = {
            'sessions': [],
            'engines': [{'name': 'antigravity'}, {'name': 'opencode'}],
            'quota': {'pools': [
                {'provider': 'gemini', 'harness': ['gemini'], 'state': 'fresh', 'remaining_percent': 100},
                {'provider': 'go', 'harness': ['opencode'], 'state': 'fresh', 'remaining_percent': 80},
            ]},
        }
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin)+':'+os.environ['PATH'], 'LOOP_INTERVAL': 'off', 'MIN_RESTART_GAP': '0', 'OPS_LOCK_DIR': str(self.home),
                    'HUB_PARSER_ROOT': str(HOST_SRC)}
        self.env.update({'ORCH_ENGINE': 'gemini', 'ORCH_FALLBACK_ENGINE': 'opencode'})

    def tearDown(self):
        self.assertNotIn('a start', self.calls())  # phase 6.1: no direct native start, ever

    def session(self, alive=False, pid=2147483647):
        # A pre-migration coordinator this script once started directly.
        return dict(id='old', tag='orchestrator-main', workspace=str(self.workspace), phase='running', worker_alive=alive, worker_pid=pid, workload_pid=pid,
                    labels={'ops.role': 'orchestrator', 'hub.intake': '67847fde-a00a-4689-8460-fbad45599f99'})

    def run_ctl(self, command='check'):
        self.runtime.write_text(json.dumps(self.data))
        # This controller launches several real shell adapters. Give it enough
        # room on a busy shared test host while retaining a bounded hang guard.
        result = subprocess.run(['bash', str(CTL), 'orchestrator', command], env=self.env, text=True, capture_output=True, timeout=60)
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

    def test_permissions_prompt_uses_hex_without_raw(self):
        self.data['capture_prompt'] = 'Yes, I accept Bypass Permissions mode\nNo, exit'
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('a send new --hex 32', self.calls())
        self.assertIn('a send new --hex 0d', self.calls())
        self.assertNotIn('--raw --hex', self.calls())

    def test_trust_prompt_uses_hex_without_raw(self):
        self.data['capture_prompt'] = 'Do you trust this folder? Yes, proceed'
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('a send new --hex 0d', self.calls())
        self.assertNotIn('--raw --hex', self.calls())

    def test_falls_back_to_fresh_hub_routed_engine_without_claude(self):
        self.data['quota'] = {'pools': [
            {'provider': 'gemini', 'harness': ['gemini'], 'state': 'fresh', 'remaining_percent': 0},
            {'provider': 'go', 'harness': ['opencode'], 'state': 'fresh', 'remaining_percent': 80},
        ]}
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('--engine go', self.starts()[-1])
        self.assertNotIn('--engine claude', self.starts()[-1])
        self.assertNotIn('--engine sonnet', self.starts()[-1])

    def test_dead_worker_recovers_when_hub_quota_command_fails(self):
        self.data['sessions'] = [self.session()]
        self.data['quota_error'] = 1
        result = self.run_ctl()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Hub quota unavailable; using local engine antigravity', result.stdout)
        self.assertIn('--engine antigravity', self.starts()[-1])
        self.assertNotIn('recovery held', result.stdout)

    def test_dead_worker_recovers_when_hub_returns_release_bootstrap_stub(self):
        self.data['sessions'] = [self.session()]
        self.data['quota'] = {'error': 'release_bootstrap', 'dispatch_enabled': False}
        result = self.run_ctl()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Hub quota unavailable; using local engine antigravity', result.stdout)
        self.assertIn('--engine antigravity', self.starts()[-1])
        self.assertNotIn('recovery held', result.stdout)

    def test_surviving_workload_blocks_forget_and_launch(self):
        self.data['sessions'] = [self.session(pid=os.getpid())]
        result = self.run_ctl()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('a forget', self.calls())
        self.assertEqual(self.starts(), [])

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
        self.assertEqual(self.starts(), [])

    def test_stop_failure_propagates_manual_restart(self):
        self.data['sessions'] = [self.session(True, os.getpid())]
        self.assertNotEqual(self.run_ctl('restart').returncode, 0)
        self.assertEqual(self.starts(), [])

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
        parts = (self.state / 'proof_due').read_text().split()
        rest = ' '.join(parts[1:]) if len(parts) > 1 else '1 gemini'
        (self.state / 'proof_due').write_text(f'{start + 600} {rest}\n')
        shifted = []
        for line in self.ticks.read_text().splitlines():
            entry = json.loads(line)
            if 'ts' not in entry: continue
            t = calendar.timegm(time.strptime(entry['ts'], '%Y-%m-%dT%H:%M:%SZ')) - 1200
            shifted.append(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))}) + '\n')
        self.ticks.write_text(''.join(shifted))

    def due_now(self):
        due = self.state / 'proof_due'
        parts = due.read_text().split()
        rest = ' '.join(parts[1:])
        due.write_text(f'{int(time.time()) - 1} {rest}\n')

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
        self.assertIn('--engine go', self.starts()[-1])
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
        # Tick timestamps have second precision, so conversion and elapsed-time
        # calculation can round the exact 1800-second fixture up by two seconds.
        self.assertRegex(self.run_ctl().stdout, r'STALLED session=live: no tick for 180[0-2]s')

    def test_detached_children_never_inherit_the_role_lock(self):
        # A detached child of the Hub CLI (or of systemd-run) can keep inherited
        # fds (Codex round-3 repro). Make the adapters do the same, then require
        # that the next check still gets the lock and no detached child holds fd 9.
        run = self.bin / 'systemd-run'
        run.write_text(run.read_text().replace("subprocess.call(args[args.index('--')+1:])", "subprocess.call(args[args.index('--')+1:], close_fds=False)"))
        hub = self.bin / 'hub'
        marker = "        if ns.sessions_cmd=='start':\n"
        self.assertIn(marker, hub.read_text())
        hub.write_text(hub.read_text().replace(marker, marker + "            import subprocess\n            child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'],close_fds=False,start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n            (p/'detached_pid').write_text(str(child.pid))\n"))
        self.env['ROLE_LOCK_WAIT'] = '1'
        self.data['sessions'] = [self.session()]
        first = self.run_ctl()
        pid = int((self.home / 'detached_pid').read_text())
        self.addCleanup(lambda: os.kill(pid, 15) if os.path.exists(f'/proc/{pid}') else None)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        lock = str(self.home / 'ops-ctl-orchestrator.lock')
        held = [os.readlink(f'/proc/{pid}/fd/{fd}') for fd in os.listdir(f'/proc/{pid}/fd')]
        self.assertNotIn(lock, held)
        second = self.run_ctl()
        self.assertNotEqual(second.returncode, 75, second.stdout)
        self.assertNotIn('role lock busy', second.stdout)

    def starts(self):
        return [line for line in self.calls().splitlines() if line.startswith('hub sessions start')]

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
        self.assertIn('--engine antigravity', self.starts()[-1])
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 1)', result.stdout)
        self.assertIn('recovery path 2 started', result.stdout)
        self.assertIn('--engine go', self.starts()[-1])
        needs = self.home / 'tmp/agent-reports/orchestrator/NEEDS_ALEXEY.md'
        self.assertFalse(needs.exists())  # one failed recovery is not an alert
        self.assertIn('--state proof_failed --recovery-failures 1', self.calls())
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 2)', result.stdout)
        self.assertIn('automatic recovery failed 2 consecutive times', needs.read_text())
        self.assertIn('--recovery-failures 2', self.calls())
        self.assertIn('--engine antigravity', self.starts()[-1])  # keeps recovering after the alert
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


    # Phase 6.1: coordinators start only through the maintained Hub CLI and are
    # discovered/controlled only through the binding Hub returns.
    def binding(self):
        return json.loads((self.state / 'hub_binding.json').read_text())

    def test_fake_hub_enforces_the_maintained_cli_parser(self):
        # The adapter parses argv with the real `hub sessions` parser, so the
        # unsupported --tag/--json of the old draft would fail every start test.
        self.runtime.write_text(json.dumps(self.data))
        for bad in (['--tag', 'orchestrator-main'], ['--json']):
            result = subprocess.run([str(self.bin / 'hub'), 'sessions', 'start', '--workspace', str(self.workspace),
                                     '--interactive', '--key', 'k', '--intake-task-id', 'i', *bad],
                                    env=self.env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('unrecognized arguments', result.stderr)

    def test_start_uses_supported_hub_cli_and_records_exact_binding(self):
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        start = self.starts()[-1]
        for flag in ('--tag', '--json', '--no-wait'):
            self.assertNotIn(flag, start.split())
        self.assertIn('--workspace ' + str(self.workspace), start)
        self.assertIn('--name orchestrator-main', start)
        self.assertIn('--intake-task-id 67847fde-a00a-4689-8460-fbad45599f99', start)
        self.assertIn('--interactive', start)
        self.assertEqual(self.binding(), {'hub_id': 'hub-0001', 'native_id': 'new', 'native_tag': 'task-hub2-hub-0001',
                                          'workspace': str(self.workspace)})
        self.assertIn('Hub session hub-0001 running as native new (tag task-hub2-hub-0001)', result.stdout)
        self.assertIn('a send new Run the orchestrator tick now', self.calls())
        self.assertIn('Hub session hub-0001 (name orchestrator-main)', self.calls())
        self.assertNotIn('systemd-run --user --scope', self.calls())
        self.assertFalse((self.state / 'hub_start_key').exists())

    def test_hub_started_session_is_found_only_by_its_binding(self):
        self.data['resolved_workspace'] = str(self.workspace.parents[1])  # Hub's resolved workspace, not $RUNDIR/$SUB
        self.assertEqual(self.run_ctl('start').returncode, 0)
        # Decoys: a Hub session reusing the old tag, and another Hub task in the same workspace.
        self.data['sessions'] += [
            dict(id='decoy-tag', tag='orchestrator-main', workspace=str(self.workspace), phase='running', worker_alive=True,
                 labels={'hub.session': 'hub-9999', 'hub.managed': 'true'}),
            dict(id='decoy-hub', tag='task-hub2-hub-9999', workspace=str(self.workspace.parents[1]), phase='running',
                 worker_alive=True, labels={'hub.session': 'hub-9999', 'hub.managed': 'true'}),
        ]
        self.tick(after_start=5)
        result = self.run_ctl()
        self.assertIn('OK session=new', result.stdout)
        self.assertEqual(len(self.starts()), 1)
        # Without the binding no guessed tag or workspace identifies the session.
        (self.state / 'hub_binding.json').unlink()
        self.assertIn('not running', self.run_ctl('status').stdout)

    def test_hub_refusal_is_typed_and_never_bypassed_then_stopped_through_hub(self):
        self.data['start_error'] = 'refused'
        result = self.run_ctl('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Hub start failed (rc=1): session_start_failed; session hub-0001 reached refused (no_capacity)', result.stdout)
        self.assertIn('recovery held', result.stdout)
        self.assertNotIn('started session', result.stdout)
        self.assertEqual(self.binding(), {'hub_id': 'hub-0001'})
        self.data['start_error'] = None
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('hub sessions stop hub-0001', self.calls())
        self.assertIn('Hub session hub-0001 already ended (refused)', result.stdout)
        self.assertEqual(self.binding()['hub_id'], 'hub-0002')

    def test_uncertain_start_reuses_key_and_typed_http_refusal_drops_it(self):
        self.data['start_error'] = 'transport'
        self.assertNotEqual(self.run_ctl('start').returncode, 0)
        key = (self.state / 'hub_start_key').read_text().split()[0]
        self.assertNotEqual(self.run_ctl('start').returncode, 0)
        keys = re.findall(r'--key (\S+)', '\n'.join(self.starts()))
        self.assertEqual(keys, [key, key])  # a lost response replays, never duplicates
        self.data['start_error'] = 'http'
        result = self.run_ctl('start')
        self.assertIn('status 409 (no_host_capacity)', result.stdout)
        self.assertFalse((self.state / 'hub_start_key').exists())
        self.data['start_error'] = None
        self.assertEqual(self.run_ctl('start').returncode, 0)
        self.assertNotEqual(re.findall(r'--key (\S+)', self.starts()[-1])[0], key)

    def test_restart_stops_bound_session_through_hub_not_native_kill(self):
        self.assertEqual(self.run_ctl('start').returncode, 0)
        result = self.run_ctl('restart')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('hub sessions stop hub-0001 --key ops-stop-hub-0001', self.calls())
        self.assertNotIn('a kill new', self.calls())
        self.assertEqual(self.binding()['native_id'], 'new2')

    def test_hub_stop_failure_holds_recovery(self):
        self.assertEqual(self.run_ctl('start').returncode, 0)
        self.data['stop_error'] = True
        result = self.run_ctl('restart')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Hub stop of hub-0001 failed: stop_not_running', result.stdout)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.binding()['native_id'], 'new')

    def test_pre_migration_coordinator_is_stopped_before_the_first_hub_start(self):
        self.data['sessions'] = [self.session(True, os.getpid())]
        self.data['kill_ok'] = True
        result = self.run_ctl('restart')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertLess(calls.index('a kill old'), calls.index('hub sessions start'))
        self.assertEqual(self.binding()['native_id'], 'new')

    def test_legacy_tag_without_own_role_label_is_not_this_coordinator(self):
        self.data['sessions'] = [{**self.session(True, os.getpid()), 'labels': {}}]
        self.assertIn('not running', self.run_ctl('status').stdout)

    def test_corrupt_binding_fails_census_instead_of_starting(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'hub_binding.json').write_text('{not json')
        result = self.run_ctl('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('corrupt Hub binding', result.stdout + result.stderr)
        self.assertIn('session census failed', result.stdout)
        self.assertEqual(self.starts(), [])

if __name__ == '__main__':
    unittest.main()
