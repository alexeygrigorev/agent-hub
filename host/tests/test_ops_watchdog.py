"""Exercise the real ops shell controller with isolated runtime/OS adapters."""
import json
import re
import os
from pathlib import Path
import subprocess
import tempfile
import stat
import signal
import shutil
import ast
import time
import unittest

CTL = Path(__file__).resolve().parents[2] / 'ops/lib/session-ctl.sh'
ADAPTER = r'''#!/usr/bin/env python3
import hashlib,json,os,pathlib,sys,uuid
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
p=pathlib.Path(os.environ['HOME'])
f=p/'runtime.json'
s=json.loads(f.read_text())
with (p/'calls').open('a') as out: out.write(name+' '+ ' '.join(args)+'\n')
def save(): f.write_text(json.dumps(s))
def detached_start(home, launch, read_birth, publish):
    # Intent precedes possible spawn. The unreaped direct Popen remains ours
    # until publication succeeds; failures settle it before relinquishing it.
    publish(home/'detached-intent.json', {'possible_start': True})
    child=None;published=False
    try:
        child=launch()
        birth=read_birth(child.pid)
        publish(home/'detached_pid', {'pid':child.pid,'birth':birth})
        published=True
    finally:
        if child is not None and not published:
            try:
                if child.poll() is None:child.terminate()
                child.wait(timeout=3)
            except BaseException:
                publish(home/'detached-uncertain.json', {'cleanup_confirmed': False})
                raise
            publish(home/'detached-reaped.json', {'reaped': True})
    return child
if name=='a':
    cmd=args[0]
    if cmd in ('start','kill','forget','send','engines'): sys.exit(97) # native mutations/fallback forbidden
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
    elif cmd=='start':
        if s.get('start_error'): sys.exit(1)
        s['sessions'].append(dict(id='new',tag='orchestrator-main',workspace=str(p/'git/agent-hub-wt-orchestrator-run/ops/orchestrator'),phase='running',worker_alive=True,worker_pid=os.getppid(),workload_pid=os.getppid()))
        save()
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
    def option(k): return args[args.index(k)+1]
    def stable(prefix,project,intake,key):
        h=hashlib.sha256(json.dumps([prefix,project,intake,key],separators=(',',':')).encode()).hexdigest()
        return h[:8]+'-'+h[8:12]+'-5'+h[13:16]+'-a'+h[17:20]+'-'+h[20:32]
    if args[:2]==['sessions','start']:
        project=option('--repo');intake=option('--intake-task-id');key=option('--key');requested=option('--workspace')
        digest=hashlib.sha256(json.dumps({'intake_id':intake,'project_id':project},sort_keys=True).encode()).hexdigest()
        native=stable('fixture-native',project,intake,key)
        s['managed']=dict(id=stable('hub2-start-session-v1',project,intake,key),operation_id=stable('hub2-start-operation-v1',project,intake,key),project_id=project,intake_id=intake,machine_id=option('--machine'),epoch=7,native_id=native,managed=True,state='running',requested_workspace=requested,workspace=requested+'/.local/hub-worktrees/'+digest)
        if s.get('start_error'):s['managed'].update(state='failed',native_id=None)
        if s.get('detach_child'):
            import subprocess
            def publish(path, value):path.write_text(json.dumps(value))
            def birth(pid):return pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
            def launch():return subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'],close_fds=False,start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            detached_start(p,launch,birth,publish)
        save()
        print(json.dumps({'session':s['managed']}));sys.exit(0)
    if args[:2]==['sessions','show']:
        if s.get('show_error'):sys.exit(1)
        row=dict(s['managed'])
        if s.get('foreign_binding'):row['project_id']='foreign/project'
        print(json.dumps({'session':row}));sys.exit(0)
    if args[:2]==['sessions','send']:
        body=sys.stdin.read()
        if s.get('send_error'):sys.exit(1)
        s.setdefault('effects',{}).setdefault(option('--key'),body);save()
        print(json.dumps({'command_wait':{'state':'acked','ack':{'outcome':'delivered'}}}));sys.exit(0)
    if args[:2]==['sessions','capture']:
        print(json.dumps({'command_wait':{'state':'acked','ack':{'outcome':'delivered','capture':s.get('capture_prompt','normal ready prompt')}}}));sys.exit(0)
    if args[:2]==['sessions','stop']:
        if s.get('stop_error'):sys.exit(1)
        if args[2]!=s['managed']['id']:sys.exit(98)
        s['managed'].update(state='stopped',stop_confirmed=True,stop_requested=True,native_exit_confirmed=True,containment_empty=True);save()
        print(json.dumps({'stop_wait':s['managed']}));sys.exit(0)
    if args and args[0]=='quota':
        default={'pools': [
            {'provider': 'gemini', 'harness': ['antigravity'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
            {'provider': 'go', 'harness': ['opencode'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
            {'provider': 'grok', 'harness': ['grok'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
            {'provider': 'zai', 'harness': ['zcodex'], 'state': 'fresh', 'remaining_percent': 80, 'limit_reached': False},
        ]}
        print(json.dumps(s.get('quota',default)))
        sys.exit(s.get('quota_error',0))
    if args[:1]!=['ops']:sys.exit(99) # fail closed outside explicit fake protocol
    sys.exit(s.get('hub_error',0))
elif name=='git':
    if 'rev-parse' in args:
        print(str(p/'exclude') if '--git-path' in args else 'abcdef')
'''


def cleanup_fixture_directory(home, settle_child, remove):
    # No finally/remove: failed custody or settlement deliberately keeps scratch.
    settle_child()
    remove(home)


class OpsWatchdogTest(unittest.TestCase):
    def cleanup_fixture(self):
        if self.home is None:
            return
        cleanup_fixture_directory(self.home, self.settle_detached_child, shutil.rmtree)

    def settle_detached_child(self):
        home = self.home
        if not (home / 'detached-intent.json').exists():
            return
        # A spawning adapter may already have reaped its direct child after a
        # failed publication. Only its explicit successful receipt settles it.
        reaped = home / 'detached-reaped.json'
        if reaped.exists():
            if json.loads(reaped.read_text()) == {'reaped': True}:
                return
            raise RuntimeError('owned_fixture_cleanup_unconfirmed_scratch_retained')
        try:
            record = json.loads((home / 'detached_pid').read_text())
            pid, birth = record['pid'], record['birth']
            if type(pid) is not int or pid <= 0 or type(birth) is not str or not birth.isdigit():
                raise ValueError('invalid custody')
        except (OSError, ValueError, TypeError, KeyError):
            raise RuntimeError('owned_fixture_custody_unknown_scratch_retained') from None
        def current():
            try:
                fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            except FileNotFoundError:
                return None
            if fields[19] != birth:
                return None # original incarnation gone; never signal a reused PID
            return fields[0]
        if current() in (None, 'Z'):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if current() in (None, 'Z'):
                return
            time.sleep(0.02)
        raise RuntimeError('owned_fixture_live_child_scratch_retained')

    def detached_helper(self):
        # Extract only the actual adapter function; no adapter top-level I/O.
        module = ast.parse(ADAPTER)
        function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'detached_start')
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<synthetic-detached-helper>', 'exec'), namespace)
        return namespace['detached_start']

    def publication_failure_control(self, failure):
        events = []
        class Child:
            pid = 1234
            def poll(self):return None
            def terminate(self):events.append('terminate')
            def wait(self, timeout):
                events.append(('wait', timeout))
                if failure == 'cleanup':raise TimeoutError('synthetic')
                return 0
        def launch():events.append('spawn');return Child()
        def birth(pid):
            events.append('birth')
            if failure == 'birth':raise ValueError('synthetic')
            return '42'
        def publish(path, value):
            events.append(path.name)
            if path.name == 'detached_pid':raise ValueError('synthetic')
        with self.assertRaises((ValueError, TimeoutError)):
            self.detached_helper()(self.home, launch, birth, publish)
        self.assertLess(events.index('detached-intent.json'), events.index('spawn'))
        self.assertIn('terminate', events)
        self.assertIn(('wait', 3), events)
        if failure == 'cleanup':
            self.assertIn('detached-uncertain.json', events)
            self.assertNotIn('detached-reaped.json', events)
        else:
            self.assertIn('detached-reaped.json', events)

    def test_detached_birth_read_failure_reaps_direct_child(self):
        self.publication_failure_control('birth')

    def test_detached_receipt_publication_failure_reaps_direct_child(self):
        self.publication_failure_control('publication')

    def test_detached_unreaped_child_retains_scratch(self):
        self.publication_failure_control('cleanup')
        removed = []
        def unresolved():raise RuntimeError('synthetic cleanup unknown')
        with self.assertRaises(RuntimeError):
            cleanup_fixture_directory(self.home, unresolved, lambda p: removed.append(p))
        self.assertEqual(removed, [])

    def test_detached_missing_or_malformed_custody_retains_scratch(self):
        # Separate nested owned fixture, no process launch or real signal.
        nested = self.home / 'negative-custody'
        nested.mkdir(mode=0o700)
        (nested / 'detached-intent.json').write_text('{"possible_start":true}')
        actual = self.home
        for bad in (None, '{}', '{"pid":0,"birth":"42"}'):
            with self.subTest(receipt=bad):
                receipt = nested / 'detached_pid'
                if bad is not None:receipt.write_text(bad)
                self.home = nested
                try:
                    with self.assertRaises(RuntimeError):self.cleanup_fixture()
                    self.assertTrue(nested.is_dir())
                    self.assertTrue((nested / 'detached-intent.json').is_file())
                finally:
                    self.home = actual

    def setUp(self):
        # The production managed ledger rejects /tmp and writable ancestors.
        # Create only test-owned leaves; never chmod a pre-existing parent.
        self.old_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.old_umask)
        repo = CTL.parents[2]
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in repo.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
                st = os.fstat(fd)
                if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
                    raise RuntimeError('watchdog_test_parent_untrusted')
            for name in ('.local', 'tmp'):
                try: os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError: pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
                st = os.fstat(fd)
                if st.st_uid != os.getuid() or st.st_mode & 0o022:
                    raise RuntimeError('watchdog_test_parent_untrusted')
        finally:
            os.close(fd)
        self.home = None
        self.addCleanup(self.cleanup_fixture) # before any unique scratch creation/spawn
        self.home = Path(tempfile.mkdtemp(prefix='ops-watchdog-', dir=repo / '.local/tmp'))
        self.bin = self.home / 'bin'
        self.bin.mkdir(mode=0o700)
        for cmd in ('a', 'git', 'gh', 'sleep', 'systemd-run', 'systemctl', 'hub'):
            file = self.bin / cmd
            file.write_text(ADAPTER)
            file.chmod(0o700)
        (self.home / 'git/bots-management-ops/.git').mkdir(parents=True)
        self.workspace = self.home / 'git/agent-hub-wt-orchestrator-run/ops/orchestrator'
        self.workspace.mkdir(parents=True)
        (self.workspace.parents[1] / '.git').touch()
        (self.home / 'exclude').touch()
        self.runtime = self.home / 'runtime.json'
        self.state = self.home / 'tmp/agent-reports/orchestrator/state'
        self.ticks = self.home / 'tmp/agent-reports/orchestrator-ticks.jsonl'
        self.ticks.parent.mkdir(parents=True)
        self.ticks.write_text('{}\n')
        self.data = {'sessions': []}
        self.env = {'HOME': str(self.home), 'TMPDIR': str(self.home),
                    'PATH': str(self.bin) + ':/usr/bin:/bin', 'LOOP_INTERVAL': 'off',
                    'MIN_RESTART_GAP': '0', 'OPS_LOCK_DIR': str(self.home),
                    'OPS_HUB_MACHINE': 'fixture-machine', 'PYTHONDONTWRITEBYTECODE': '1',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                    'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0'}

    def session(self, alive=False, pid=2147483647):
        return dict(id='old', tag='orchestrator-main', workspace=str(self.workspace), phase='running', worker_alive=alive, worker_pid=pid, workload_pid=pid)

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
        self.assertIn('already running legacy session: old', result.stdout)
        self.assertNotIn('git ', self.calls())
        self.assertEqual(self.starts(), [])
        self.assertNotIn('a kill', self.calls())

    def test_dead_record_forget_and_real_start(self):
        # An unowned legacy record is not a managed retirement proof.
        self.data['sessions'] = [self.session()]
        result = self.run_ctl()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('acknowledged owner retirement', result.stdout)
        self.assertNotIn('a forget', self.calls())
        self.assertNotIn('a kill', self.calls())
        self.assertEqual(self.starts(), [])
        self.assertFalse((self.state / 'proof_due').exists())
        # Model a separately acknowledged owner retirement, not controller kill.
        self.data['sessions'] = []
        result = self.run_ctl()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('recovery path 1 started on hub', result.stdout)
        self.assertTrue((self.state / 'proof_due').exists())
        self.assertEqual(len(self.starts()), 1)

    def test_permissions_prompt_uses_hex_without_raw(self):
        # Pane text never authorizes a permission/paid-menu gesture.
        self.data['capture_prompt'] = 'Yes, I accept Bypass Permissions mode\nNo, exit'
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('hub sessions send', self.calls())
        self.assertNotIn('a send', self.calls())
        self.assertNotIn('--hex', self.calls())
        self.assertNotIn('--raw', self.calls())
        self.assertEqual(len(self.data['effects']), 1)

    def test_trust_prompt_uses_hex_without_raw(self):
        self.data['capture_prompt'] = 'Do you trust this folder? Yes, proceed'
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('hub sessions send', self.calls())
        self.assertNotIn('a send', self.calls())
        self.assertNotIn('--raw', self.calls())
        self.assertNotIn('--hex', self.calls())
        self.assertEqual(len(self.data['effects']), 1)

    def test_falls_back_to_fresh_hub_routed_engine_without_claude(self):
        self.data['quota'] = {'pools': [{'provider': 'go', 'remaining_percent': 80}]}
        result = self.run_ctl('start')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        start = self.starts()[-1]
        self.assertIn('--machine fixture-machine', start)
        for override in ('--engine', '--model', '--profile', '--reason', '--force'):
            self.assertNotIn(override, start)
        self.assertNotIn('a start', self.calls())
        self.assertNotIn('a engines', self.calls())

    def test_dead_worker_recovers_when_hub_quota_command_fails(self):
        # The wrapper does not pre-clear quotas or pick a native fallback.
        self.data['quota_error'] = 1
        result = self.run_ctl()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('recovery path 1 started on hub', result.stdout)
        self.assertEqual(len(self.starts()), 1)
        self.assertNotIn('hub quota', self.calls())
        self.assertNotIn('a start', self.calls())

    def test_dead_worker_recovers_when_hub_returns_release_bootstrap_stub(self):
        # A failed authenticated Hub read holds, never becomes empty census.
        self.data['show_error'] = True
        self.data['quota'] = {'error': 'release_bootstrap', 'dispatch_enabled': False}
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertEqual(len(self.starts()), 1)
        self.assertFalse((self.state / 'proof_due').exists())
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertEqual(len(self.starts()), 1) # original key retained; no replay
        self.assertNotIn('a start', self.calls())
        self.assertNotIn('hub sessions stop', self.calls())

    def test_surviving_workload_blocks_forget_and_launch(self):
        self.data['sessions'] = [self.session(pid=os.getpid())]
        result = self.run_ctl()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('a forget', self.calls())
        self.assertNotIn('a start', self.calls())
        self.assertEqual(self.starts(), [])

    def test_unknown_pid_blocks_forget(self):
        session = self.session()
        del session['workload_pid']
        self.data['sessions'] = [session]
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertNotIn('a forget', self.calls())
        self.assertEqual(self.starts(), [])

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
        result = self.run_ctl()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('hub sessions stop', self.calls())
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '1')
        self.tick(after_start=5)
        result = self.run_ctl()
        self.assertIn('recovery proven', result.stdout)
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '0')

    def test_census_error_never_starts_duplicate(self):
        self.data['census_error'] = True
        self.assertNotEqual(self.run_ctl().returncode, 0)
        self.assertNotIn('a start', self.calls())
        self.assertEqual(self.starts(), [])

    def test_stop_failure_propagates_manual_restart(self):
        self.assertEqual(self.run_ctl('start').returncode, 0)
        original = self.data['managed']['id']
        self.data['stop_error'] = True
        self.assertNotEqual(self.run_ctl('restart').returncode, 0)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.data['managed']['id'], original)
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
        self.run_ctl()
        self.tick(after_start=700)
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 1)', result.stdout)
        self.assertIn('recovery path 2 started on hub', result.stdout)
        self.assertEqual(len(self.starts()), 2)
        self.assertIn('hub sessions stop', self.calls())
        self.assertTrue(all('--engine' not in start for start in self.starts()))
        self.assertEqual((self.state / 'restart_failures').read_text().strip(), '1')

    def test_on_time_tick_in_history_proves_even_after_a_later_tick(self):
        self.run_ctl()
        self.tick(after_start=120)
        self.tick(after_start=700)
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
        self.data['detach_child'] = True
        self.env['ROLE_LOCK_WAIT'] = '1'
        first = self.run_ctl()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        pid = json.loads((self.home / 'detached_pid').read_text())['pid']
        lock = str(self.home / 'ops-ctl-orchestrator.lock')
        held = [os.readlink(f'/proc/{pid}/fd/{fd}') for fd in os.listdir(f'/proc/{pid}/fd')]
        self.assertNotIn(lock, held)
        second = self.run_ctl()
        self.assertNotEqual(second.returncode, 75, second.stdout)
        self.assertNotIn('role lock busy', second.stdout)
        self.assertEqual(len(self.starts()), 1)

    def starts(self):
        return [line for line in self.calls().splitlines() if line.startswith('hub sessions start')]

    def test_29_oct_live_session_without_ticks_is_restarted_not_ok(self):
        self.assertEqual(self.run_ctl('start').returncode, 0)
        original = self.data['managed']['id']
        (self.state / 'last_start').write_text(str(int(time.time()) - 33091))
        self.ticks.unlink()
        result = self.run_ctl()
        self.assertIn('STALLED session=', result.stdout)
        self.assertIn('no tick for 3309', result.stdout)
        self.assertNotIn('OK session=', result.stdout)
        self.assertIn('recovery path 1 started on hub', result.stdout)
        self.assertIn('hub sessions stop ' + original, self.calls())
        self.assertNotEqual(self.data['managed']['id'], original)
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
        self.run_ctl()
        native = self.data['managed']['native_id']
        delays = self.timer_delays('proof')
        self.assertEqual(len(delays), 1)
        self.assertTrue(590 <= delays[0] <= 601, delays)
        result = self.run_ctl()
        self.assertIn('recovery pending proof: session=' + native, result.stdout)
        self.assertEqual(len(self.starts()), 1)
        self.tick(after_start=120)
        result = self.run_ctl()
        self.assertIn('recovery proven: fresh tick 120s after start (path 1)', result.stdout)
        self.assertFalse((self.state / 'proof_due').exists())
        self.assertIn('OK session=' + native, result.stdout)

    def test_unproven_restart_starts_second_path_then_alerts_after_two(self):
        self.run_ctl()
        first = self.data['managed']['id']
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 1)', result.stdout)
        self.assertIn('recovery path 2 started on hub', result.stdout)
        self.assertIn('hub sessions stop ' + first, self.calls())
        second = self.data['managed']['id']
        self.assertNotEqual(first, second)
        needs = self.home / 'tmp/agent-reports/orchestrator/NEEDS_ALEXEY.md'
        self.assertFalse(needs.exists())
        self.assertIn('--state proof_failed --recovery-failures 1', self.calls())
        self.due_now()
        result = self.run_ctl()
        self.assertIn('recovery proof FAILED (path 2)', result.stdout)
        self.assertIn('automatic recovery failed 2 consecutive times', needs.read_text())
        self.assertIn('--recovery-failures 2', self.calls())
        self.assertEqual(len(self.starts()), 3)
        self.assertTrue(all('--engine' not in start for start in self.starts()))
        self.assertNotIn('a start', self.calls())

    def test_session_dying_inside_proof_window_fails_proof_immediately(self):
        self.run_ctl()
        original = self.data['managed']['id']
        self.data['managed']['state'] = 'failed'
        result = self.run_ctl()
        self.assertIn('owned terminal Hub session; qualified STOP required', result.stdout)
        self.assertIn('hub sessions stop ' + original, self.calls())
        self.assertIn('recovery path 1 started on hub', result.stdout)
        self.assertEqual(len(self.starts()), 2)
        self.assertNotEqual(self.data['managed']['id'], original)

    def test_hub_report_failure_never_blocks_recovery(self):
        self.data['hub_error'] = 1 # fake ops-report only, not session transport
        result = self.run_ctl()
        self.assertIn('hub ops report failed', result.stdout)
        self.assertIn('recovery path 1 started on hub', result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.starts()), 1)


def load_tests(loader, standard_tests, pattern):
    # Required deploy CI selects this module explicitly. Keep the new installed
    # layout cases in their authoritative module; lazy import avoids exporting
    # an imported TestCase that pytest would collect a second time.
    if __package__:
        from .test_ops_install import OpsInstallTests
    else:
        from test_ops_install import OpsInstallTests
    installed = loader.loadTestsFromTestCase(OpsInstallTests)
    if installed.countTestCases() != 6:
        raise RuntimeError('ops_installed_layout_suite_changed_or_empty')
    standard_tests.addTests(installed)
    return standard_tests


if __name__ == '__main__':
    unittest.main()
