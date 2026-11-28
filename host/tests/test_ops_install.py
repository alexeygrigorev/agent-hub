"""Standard Ops install layout, using only synthetic HOME/Hub/service adapters."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / 'ops/install.sh'
FAKE = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
home=pathlib.Path(os.environ['HOME']);args=sys.argv[1:];name=pathlib.Path(sys.argv[0]).name
with (home/'adapter-calls.jsonl').open('a') as f:f.write(json.dumps([name,args])+'\n')
if name=='systemctl':
    if args==['--user','show-environment']:print('PATH='+os.environ['PATH'])
    sys.exit(0)
if name!='hub':sys.exit(99) # native/provider/git/network invocation forbidden
views=json.loads((home/'views.json').read_text())
if args[:2]==['sessions','show']:
    print(json.dumps({'session':views[args[2]]}));sys.exit(0)
if args[:2]==['sessions','send']:
    if args[2] not in views or views[args[2]]['state']!='running':sys.exit(1)
    if args[args.index('--body-file')+1]!='-':sys.exit(1)
    row={'session':args[2],'key':args[args.index('--key')+1],'body':sys.stdin.read()}
    with (home/'effects.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
    print(json.dumps({'command_wait':{'state':'acked','ack':{'outcome':'delivered'}}}));sys.exit(0)
sys.exit(98)
'''


def stable(prefix, project, intake, key):
    h = hashlib.sha256(json.dumps([prefix, project, intake, key], separators=(',', ':')).encode()).hexdigest()
    return h[:8] + '-' + h[8:12] + '-5' + h[13:16] + '-a' + h[17:20] + '-' + h[20:32]


class OpsInstallTests(unittest.TestCase):
    def setUp(self):
        self.home = None
        self.children = []
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        self.addCleanup(self.cleanup_owned)
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in REPO.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
                s = os.fstat(fd)
                if s.st_uid not in (0, os.getuid()) or s.st_mode & 0o022:
                    raise RuntimeError('ops_install_test_parent_untrusted')
            for name in ('.local', 'tmp'):
                try: os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError: pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
                s = os.fstat(fd)
                if s.st_uid != os.getuid() or s.st_mode & 0o022:
                    raise RuntimeError('ops_install_test_parent_untrusted')
        finally:
            os.close(fd)
        self.home = Path(tempfile.mkdtemp(prefix='ops-install-', dir=REPO / '.local/tmp'))
        self.adapters = self.home / 'adapters'
        self.adapters.mkdir(mode=0o700)
        for command in ('systemctl', 'hub', 'a', 'gh', 'git', 'ssh', 'curl', 'msmtp', 'sendmail'):
            p = self.adapters / command
            p.write_text(FAKE)
            p.chmod(0o700)
        self.env = {'PATH': str(self.adapters) + ':/usr/bin:/bin', 'HOME': str(self.home),
                    'TMPDIR': str(self.home), 'OPS_LOCK_DIR': str(self.home),
                    'OPS_HUB_MACHINE': 'fixture-machine',
                    'OPS_INTAKE': '12345678-1234-4123-a123-123456789012',
                    'PYTHONDONTWRITEBYTECODE': '1', 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_ALLOW_PROTOCOL': 'file',
                    'GIT_TERMINAL_PROMPT': '0',
                    # Runners without a lingering systemd user (no /run/user/<uid>) cannot
                    # open the controller role lock; keep it inside the synthetic HOME.
                    'OPS_LOCK_DIR': str(self.home / 'run')}
        (self.home / 'run').mkdir(mode=0o700)
        self.views = {}

    def cleanup_owned(self):
        # No automatic scratch finalizer after a failed process cleanup.
        for child in self.children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL) # own unreaped Popen group only
                child.wait(timeout=3)
        if self.home is not None:
            shutil.rmtree(self.home)

    def invoke(self, argv):
        child = None
        try:
            child = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
            self.children.append(child)
            stdout, stderr = child.communicate(timeout=15)
            return child.returncode, (stdout + stderr).decode('utf-8', 'replace')
        finally:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)

    def install(self):
        return self.invoke(['bash', str(INSTALLER)])

    def calls(self):
        p = self.home / 'adapter-calls.jsonl'
        return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

    def seed_managed(self, role):
        # Same public deterministic identifiers/binding as actual controller;
        # a synthetic already-running row avoids any worker/start side effect.
        project = 'alexeygrigorev/agent-hub'
        intake = self.env['OPS_INTAKE']
        key = 'ops-' + role + '-' + ('1' if role == 'oncall' else '2') * 32
        requested = str(self.home / ('git/agent-hub-wt-' + role + '-run') / ('ops/' + role))
        digest = hashlib.sha256(json.dumps({'intake_id': intake, 'project_id': project}, sort_keys=True).encode()).hexdigest()
        resolved = requested + '/.local/hub-worktrees/' + digest
        native = stable('fixture-native', project, intake, key)
        session = stable('hub2-start-session-v1', project, intake, key)
        operation = stable('hub2-start-operation-v1', project, intake, key)
        binding = {'machine': 'fixture-machine', 'epoch': 7, 'native': native, 'resolved': resolved}
        record = {'v': 1, 'role': role, 'intake': intake, 'requested': requested, 'project': project,
                  'key': key, 'id': session, 'operation': operation, 'target_machine': 'fixture-machine',
                  'submitted': True, 'retired': False, 'binding': binding,
                  'assignment': {'machine': 'fixture-machine', 'epoch': 7},
                  'brief_sent': True, 'brief_sha256': hashlib.sha256(b'fixture brief').hexdigest(),
                  'stop_requested': False, 'started_at': 1}
        folder = self.home / 'tmp/agent-reports' / role / 'state/managed'
        folder.mkdir(mode=0o700, parents=True)
        (folder / 'current.json').write_text(json.dumps(record))
        (folder / 'current.json').chmod(0o600)
        self.views[session] = {'id': session, 'operation_id': operation, 'project_id': project,
                              'intake_id': intake, 'machine_id': 'fixture-machine', 'epoch': 7,
                              'native_id': native, 'managed': True, 'state': 'running',
                              'requested_workspace': requested, 'workspace': resolved}
        (self.home / 'views.json').write_text(json.dumps(self.views))
        return session

    def test_standard_install_both_send_entrypoints_use_installed_controller(self):
        (self.home / 'bin').mkdir()
        unrelated = self.home / 'bin/unrelated-operator-file'
        unrelated.write_text('preserve this\n')
        rc, output = self.install()
        self.assertEqual(rc, 0, output)
        controller = (REPO / 'ops/lib/session-ctl.sh').read_bytes()
        self.assertEqual((self.home / 'bin/session-ctl.sh').read_bytes(), controller)
        self.assertEqual((self.home / '.local/share/agent-hub-ops/session-ctl.sh').read_bytes(), controller)
        self.assertEqual(stat.S_IMODE((self.home / 'bin/session-ctl.sh').stat().st_mode), 0o755)
        for role, wrapper in [('orchestrator', 'orch-send'), ('oncall', 'oncall-send')]:
            session = self.seed_managed(role)
            rc, output = self.invoke([str(self.home / 'bin' / wrapper), 'owned ' + role + ' inbox item'])
            self.assertEqual(rc, 0, output)
            self.assertIn('via Hub (bound managed identity)', output)
            effects = [json.loads(s) for s in (self.home / 'effects.jsonl').read_text().splitlines()]
            self.assertEqual(sum(x['session'] == session for x in effects), 1)
            self.assertIn('owned ' + role + ' inbox item', effects[-1]['body'])
            self.assertEqual(len(list((self.home / 'tmp/agent-reports' / role / 'inbox').glob('*.md'))), 1)
        self.assertEqual(unrelated.read_text(), 'preserve this\n')
        self.assertFalse(any(name == 'a' for name, args in self.calls()))
        self.assertEqual(sum(args[:2] == ['sessions', 'send'] for name, args in self.calls() if name == 'hub'), 2)

    def assert_conflict_preserves_before_install(self, expected):
        rc, output = self.install()
        self.assertNotEqual(rc, 0, output)
        self.assertIn('ops_controller_companion_conflict', output)
        self.assertEqual((self.home / 'bin/session-ctl.sh').read_bytes(), expected)
        self.assertFalse((self.home / '.local/share/agent-hub-ops').exists())
        self.assertFalse((self.home / '.config').exists())
        self.assertFalse((self.home / 'bin/orch-send').exists())
        self.assertFalse((self.home / 'bin/oncall-send').exists())
        self.assertEqual(self.calls(), [])

    def test_unknown_existing_companion_refuses_before_any_install_write(self):
        (self.home / 'bin').mkdir()
        companion = self.home / 'bin/session-ctl.sh'
        companion.write_bytes(b'#!/bin/sh\nexit 17\n')
        companion.chmod(0o755)
        self.assert_conflict_preserves_before_install(companion.read_bytes())

    def test_symlink_companion_refuses_even_if_source_bytes_match(self):
        (self.home / 'bin').mkdir()
        original = self.home / 'other-owned-file'
        original.write_bytes((REPO / 'ops/lib/session-ctl.sh').read_bytes())
        (self.home / 'bin/session-ctl.sh').symlink_to(original)
        self.assert_conflict_preserves_before_install(original.read_bytes())
        self.assertTrue((self.home / 'bin/session-ctl.sh').is_symlink())

    def test_hardlinked_companion_refuses_even_if_source_bytes_match(self):
        (self.home / 'bin').mkdir()
        original = self.home / 'other-owned-file'
        original.write_bytes((REPO / 'ops/lib/session-ctl.sh').read_bytes())
        os.link(original, self.home / 'bin/session-ctl.sh')
        self.assert_conflict_preserves_before_install(original.read_bytes())
        self.assertEqual(original.stat().st_nlink, 2)

    def test_matching_owned_installed_pair_allows_normal_upgrade_and_rerun(self):
        (self.home / 'bin').mkdir()
        destination = self.home / '.local/share/agent-hub-ops'
        destination.mkdir(parents=True)
        old = b'#!/bin/sh\n# synthetic previous selected controller\nexit 17\n'
        for p in (destination / 'session-ctl.sh', self.home / 'bin/session-ctl.sh'):
            p.write_bytes(old); p.chmod(0o755)
        for iteration in range(2):
            with self.subTest(install=iteration):
                rc, output = self.install()
                self.assertEqual(rc, 0, output)
                self.assertEqual((self.home / 'bin/session-ctl.sh').read_bytes(), (REPO / 'ops/lib/session-ctl.sh').read_bytes())

    def test_installed_send_foreign_binding_retains_inbox_without_native_fallback(self):
        rc, output = self.install()
        self.assertEqual(rc, 0, output)
        session = self.seed_managed('oncall')
        self.views[session]['project_id'] = 'foreign/project'
        (self.home / 'views.json').write_text(json.dumps(self.views))
        rc, output = self.invoke([str(self.home / 'bin/oncall-send'), 'retain without sending'])
        self.assertEqual(rc, 2, output)
        self.assertIn('inbox retained', output)
        self.assertEqual(len(list((self.home / 'tmp/agent-reports/oncall/inbox').glob('*.md'))), 1)
        self.assertFalse((self.home / 'effects.jsonl').exists())
        self.assertFalse(any(name == 'a' for name, args in self.calls()))
        self.assertFalse(any(args[:2] == ['sessions', 'send'] for name, args in self.calls() if name == 'hub'))


if __name__ == '__main__':
    unittest.main()
