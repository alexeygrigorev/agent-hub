"""Real commits/tests/pushes against a private bare origin; never touches GitHub."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class PushMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.remote = self.root / 'origin.git'
        self.repo.mkdir()
        self.env = dict(os.environ)
        # Do not inherit pytest/worker Git configuration into fixture repos.
        for key in list(self.env):
            if key.startswith('GIT_') or key == 'AGENT_HUB_HEAVY_TEST_HELD':
                self.env.pop(key)
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT='0')
        self.git('init', '-q', '--initial-branch=main')
        self.git('init', '-q', '--bare', str(self.remote))
        self.git('config', 'user.name', 'Gate Fixture')
        self.git('config', 'user.email', 'gate@example.invalid')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.write('.gitignore', '__pycache__/\n')
        (self.repo / 'scripts/ci').mkdir(parents=True)
        (self.repo / 'host/tests').mkdir(parents=True)
        (self.repo / 'host/agent_hub_host').mkdir()
        # Regular packages: unittest resolves host.tests.* from this fixture even when another regular `host`
        # package (the real repo on sys.path/.pth in CI) exists; a bare namespace dir would lose to it.
        (self.repo / 'host/__init__.py').write_text('')
        (self.repo / 'host/tests/__init__.py').write_text('')
        shutil.copy(ROOT / 'scripts/push-main.sh', self.repo / 'scripts/push-main.sh')
        shutil.copy(ROOT / 'host/agent_hub_host/prepush.py', self.repo / 'host/agent_hub_host/prepush.py')
        shutil.copy(ROOT / 'scripts/ci/affected_tests.py', self.repo / 'scripts/ci/affected_tests.py')
        self.write('scripts/capped-test-retry.sh', '#!/bin/bash\nset -eu\n[ "$1" = -- ]; shift\nexec "$@"\n')
        # Lightweight fixture lane uses the production affected-test selector,
        # then really executes its selected unittest suites. No heavy subprocesses.
        self.write('scripts/test-affected-local.sh', '''#!/usr/bin/env python3
import json, pathlib, subprocess, sys
assert sys.argv[1] == '--base' and len(sys.argv) == 3
state = pathlib.Path(subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip())
(state / 'tested-base').write_text(sys.argv[2])
action = state / 'action'
mode = action.read_text() if action.exists() else ''
if mode.startswith('exit:'): sys.exit(int(mode.split(':')[1]))
if mode == 'dirty': pathlib.Path('dirty.txt').write_text('changed during tests')
if mode == 'commit':
    subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'untested'], check=True)
if mode == 'advance':
    subprocess.run(['git', '-C', '../peer', 'commit', '--allow-empty', '-qm', 'peer'], check=True)
    subprocess.run(['git', '-C', '../peer', 'push', '-q', 'origin', 'HEAD:main'], check=True)
plan = json.loads(subprocess.check_output(['python3', 'scripts/ci/affected_tests.py', sys.argv[2]]))
(state / 'selected').write_text(json.dumps(plan))
if plan['broad']: sys.exit(4)
if not plan['pytest']: sys.exit(0)
sys.exit(subprocess.call(['python3', '-m', 'unittest', *plan['pytest']]))
''')
        self.write('scripts/test-full-local.sh', '''#!/usr/bin/env python3
import pathlib, subprocess, sys
assert sys.argv[1:] == ['--priority', 'final']
state = pathlib.Path(subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip())
(state / 'full-ran').touch()
sys.exit(subprocess.call(['python3', '-m', 'unittest', 'discover', '-s', 'host/tests']))
''')
        self.write_suite(True)
        self.commit('seed')
        self.git('push', '-q', 'origin', 'HEAD:main')
        self.seed = self.tip()
        self.primary = self.repo
        worker = self.root / 'worker'
        self.git('worktree', 'add', '-qb', 'worker', str(worker))
        self.repo = worker
        self.state = Path(self.git('rev-parse', '--absolute-git-dir'))

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.repo,
                                       env=self.env, stderr=subprocess.DEVNULL, text=True).strip()

    def write(self, name, body):
        path = self.repo / name
        path.write_text(body)
        if name.startswith('scripts/'):
            path.chmod(0o755)

    def write_suite(self, passing):
        self.write('host/tests/test_fixture.py',
                   'import unittest\nclass Fixture(unittest.TestCase):\n'
                   f'    def test_result(self): self.assertTrue({passing!r})\n')

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def tip(self):
        return self.git('rev-parse', 'refs/heads/main', cwd=self.remote)

    def run_gate(self):
        return subprocess.run(['bash', 'scripts/push-main.sh'], cwd=self.repo,
                              env=self.env, capture_output=True, text=True, timeout=20)

    def candidate(self):
        self.write_suite(False)
        return self.commit('deliberately failing test')

    def assert_refused(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.tip(), self.seed)

    def test_failing_commit_refused_then_repaired_commit_pushes(self):
        failing = self.candidate()
        result = self.run_gate()
        self.assert_refused(result)
        self.assertIn('affected tests failed', result.stderr)
        self.assertEqual((self.state / 'tested-base').read_text(), self.seed)
        self.assertIn('test_fixture.py', (self.state / 'selected').read_text())
        self.write_suite(True)
        # Keep the fixture test different from the seed after fixing it.
        with (self.repo / 'host/tests/test_fixture.py').open('a') as out:
            out.write('# repaired\n')
        passing = self.commit('repair test')
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.tip(), passing)
        self.assertNotEqual(self.tip(), failing)

    def test_broad_runs_full_suite_and_refuses_real_failure(self):
        self.candidate()
        self.write('package.json', '{}\n')
        self.commit('broad change')
        self.assert_refused(self.run_gate())
        self.assertTrue((self.state / 'full-ran').exists())

    def test_broad_passing_suite_pushes(self):
        self.write('package.json', '{}\n')
        passing = self.commit('passing broad change')
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / 'full-ran').exists())
        self.assertEqual(self.tip(), passing)

    def test_other_runner_failures_never_push(self):
        self.candidate()
        for code in (1, 2, 3, 127):
            with self.subTest(code=code):
                (self.state / 'action').write_text(f'exit:{code}')
                self.assert_refused(self.run_gate())
                self.assertFalse((self.state / 'full-ran').exists())

    def passing_candidate(self):
        with (self.repo / 'host/tests/test_fixture.py').open('a') as out:
            out.write('# changed passing suite\n')
        return self.commit('passing candidate')

    def test_dirty_tree_refused_before_tests(self):
        self.passing_candidate()
        self.write('dirty.txt', 'uncommitted')
        self.assert_refused(self.run_gate())
        self.assertFalse((self.state / 'tested-base').exists())

    def test_tree_mutation_during_tests_refused(self):
        self.passing_candidate()
        (self.state / 'action').write_text('dirty')
        self.assert_refused(self.run_gate())

    def test_head_mutation_during_tests_refused(self):
        self.passing_candidate()
        (self.state / 'action').write_text('commit')
        self.assert_refused(self.run_gate())

    def test_remote_advances_during_tests_refused(self):
        self.git('clone', '-q', str(self.remote), str(self.root / 'peer'))
        peer = self.root / 'peer'
        self.git('checkout', '-q', '-B', 'main', 'origin/main', cwd=peer)
        self.git('config', 'user.name', 'Peer', cwd=peer)
        self.git('config', 'user.email', 'peer@example.invalid', cwd=peer)
        self.passing_candidate()
        candidate = self.git('rev-parse', 'HEAD')
        (self.state / 'action').write_text('advance')
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tip(), self.git('rev-parse', 'HEAD', cwd=peer))
        self.assertNotEqual(self.tip(), candidate)

    def test_affected_and_broad_tests_can_acquire_release_lock(self):
        # Real test children must be able to use this repository lock. Before
        # fbe77435's follow-up this failed because the hook held it throughout
        # the affected lane (and the BROAD fallback).
        for index, (raw, broad) in enumerate(((False, False), (True, False),
                                               (False, True), (True, True))):
            with self.subTest(raw=raw, broad=broad):
                self.write('host/tests/test_fixture.py', '''import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from host.agent_hub_host.prepush import release_lock
class Fixture(unittest.TestCase):
    def test_lock_is_available(self):
        with release_lock(Path.cwd()):
            pass
''' + f'# candidate {index}\n')
                if broad:
                    self.write('package.json', '{"version": "' + str(index) + '"}\n')
                candidate = self.commit('tests use release lock')
                if raw:
                    self.install_for_launch()
                result = self.raw_push() if raw else self.run_gate()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.tip(), candidate)
                if broad:
                    self.assertTrue((self.state / 'full-ran').exists())

    def test_busy_final_lock_does_not_prevent_tests_then_retry_pushes(self):
        from agent_hub_host.prepush import release_lock
        candidate = self.passing_candidate()
        with release_lock(self.repo):
            result = self.run_gate()
            self.assert_refused(result)
            # Admission/tests happen before the short final critical section.
            self.assertEqual((self.state / 'tested-base').read_text(), self.seed)
            self.assertTrue((self.state / 'selected').exists())
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.tip(), candidate)

    def install_for_launch(self, repo=None):
        from agent_hub_host.worktree_guard import protect_bound_worktree
        self.assertTrue(protect_bound_worktree('git', str(repo or self.repo), 'a' * 32, None))

    def raw_push(self, repo=None, ref='HEAD:main', extra_env=None):
        return subprocess.run(['git', 'push', 'origin', ref], cwd=repo or self.repo,
                              env={**self.env, **(extra_env or {})}, capture_output=True,
                              text=True, timeout=20)

    def test_launch_installs_hook_raw_failing_push_refused_then_passing_pushes(self):
        self.install_for_launch()
        self.candidate()
        self.assert_refused(self.raw_push(extra_env={'HUB_PREPUSH_SKIP': '1'}))
        self.write_suite(True)
        with (self.repo / 'host/tests/test_fixture.py').open('a') as out:
            out.write('# raw push repair\n')
        passing = self.commit('repair raw push')
        result = self.raw_push()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tip(), passing)

    def test_hook_uses_remote_main_ref_even_when_local_branch_has_another_name(self):
        self.install_for_launch()
        self.git('checkout', '-qb', 'feature')
        self.candidate()
        self.assert_refused(self.raw_push(ref='feature:main'))

    def test_hook_allows_non_main_push_without_running_tests(self):
        self.install_for_launch()
        failing = self.candidate()
        result = self.raw_push(ref='HEAD:worker')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tip(), self.seed)
        self.assertEqual(self.git('rev-parse', 'worker', cwd=self.remote), failing)
        self.assertFalse((self.state / 'tested-base').exists())

    def test_hook_broad_failure_refuses_raw_push(self):
        self.install_for_launch()
        self.candidate()
        self.write('package.json', '{}\n')
        self.commit('raw broad failure')
        self.assert_refused(self.raw_push())
        self.assertTrue((self.state / 'full-ran').exists())

    def test_revert_of_refused_commit_can_push_through_the_same_gate(self):
        self.install_for_launch()
        bad = self.candidate()
        self.assert_refused(self.raw_push())
        self.git('revert', '--no-edit', bad)
        reverted = self.git('rev-parse', 'HEAD')
        result = self.raw_push()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tip(), reverted)

    def test_hook_refuses_commit_other_than_checked_out_head(self):
        self.install_for_launch()
        passing = self.passing_candidate()
        self.git('commit', '--allow-empty', '-qm', 'different HEAD')
        self.assert_refused(self.raw_push(ref=passing + ':main'))

    def test_hook_refuses_missing_lane_and_deletion(self):
        self.install_for_launch()
        self.passing_candidate()
        self.git('rm', 'scripts/test-affected-local.sh')
        self.commit('remove lane')
        self.install_for_launch()  # Repository policy survives a removed lane.
        self.assert_refused(self.raw_push())
        self.assert_refused(self.raw_push(ref=':main'))

    def test_installer_preserves_inherited_hooks_and_main_checkout(self):
        custom = self.root / 'custom-hooks'
        custom.mkdir()
        self.git('config', '--local', 'core.hooksPath', str(custom))
        original = '#!/bin/sh\nexit 17\n'
        prior = custom / 'pre-push'
        prior.write_text(original); prior.chmod(0o755)
        marker = self.root / 'post-commit-ran'
        post = custom / 'post-commit'
        post.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n'); post.chmod(0o755)
        self.install_for_launch()
        self.assertNotEqual(self.git('config', '--get', 'core.hooksPath'), str(custom))
        self.assertEqual(self.git('config', '--get', 'core.hooksPath', cwd=self.primary), str(custom))
        self.passing_candidate()
        self.assertTrue(marker.exists(), 'inherited post-commit hook must still run')
        self.assert_refused(self.raw_push())
        self.assertEqual(prior.read_text(), original)
        prior.write_text('#!/bin/sh\nexit 0\n')
        result = self.raw_push()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_main_checkout_and_other_projects_are_not_automatically_hooked(self):
        from agent_hub_host.worktree_guard import protect_bound_worktree
        self.assertTrue(protect_bound_worktree('git', str(self.primary), None, None))
        self.assertFalse((self.primary / '.git/hooks/pre-push').exists())
        explicit = subprocess.run(['python3', 'host/agent_hub_host/prepush.py', 'install'],
                                  cwd=self.primary, env=self.env, capture_output=True)
        self.assertNotEqual(explicit.returncode, 0)
        self.install_for_launch()
        self.assertFalse((self.primary / '.git/hooks/pre-push').exists())
        check = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], cwd=self.primary,
                               env=self.env, capture_output=True)
        self.assertEqual(check.returncode, 1)

        other = self.root / 'other-project'
        self.git('init', '-q', str(other))
        (other / 'file').write_text('other project')
        self.git('add', '.', cwd=other)
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'seed', cwd=other)
        sibling = self.root / 'other-worker'
        self.git('worktree', 'add', '-qb', 'other-worker', str(sibling), cwd=other)
        self.assertTrue(protect_bound_worktree('git', str(sibling), 'a' * 32, None))
        check = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], cwd=sibling,
                               env=self.env, capture_output=True)
        self.assertEqual(check.returncode, 1)

    def test_existing_worker_rollout_protects_both_workers_but_not_main(self):
        sibling = self.root / 'rollout-sibling'
        self.git('worktree', 'add', '-qb', 'rollout', str(sibling))
        human = self.root / 'human-integration'
        self.git('worktree', 'add', '-qb', 'human', str(human))
        self.git('worktree', 'lock', '--reason', 'manual integration', str(human))
        unbound = self.root / 'unbound'
        self.git('worktree', 'add', '-qb', 'unbound', str(unbound))
        for worker, identity in ((self.repo, 'a' * 32), (sibling, 'b' * 32)):
            self.git('worktree', 'lock', '--reason', 'hub-session ' + identity + ' intake fixture', str(worker))
        result = subprocess.run(['python3', 'host/agent_hub_host/prepush.py', 'install-workers'],
                                cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2 worker worktrees', result.stdout)
        self.assertFalse((self.primary / '.git/hooks/pre-push').exists())
        for untouched in (human, unbound):
            check = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], cwd=untouched,
                                   env=self.env, capture_output=True)
            self.assertEqual(check.returncode, 1, 'human/unbound hook settings must stay unchanged')
        self.candidate()
        self.assert_refused(self.raw_push())
        suite = sibling / 'host/tests/test_fixture.py'
        suite.write_text('import unittest\nclass Fixture(unittest.TestCase):\n'
                         '    def test_result(self): self.assertTrue(False)\n')
        self.git('commit', '-qam', 'failing rollout sibling', cwd=sibling)
        self.assert_refused(self.raw_push(sibling))

    def test_created_and_adopted_worktrees_each_get_the_hook(self):
        sibling = self.root / 'sibling'
        self.git('worktree', 'add', '-qb', 'sibling', str(sibling))
        self.install_for_launch()
        self.install_for_launch(sibling)
        self.candidate()
        self.assert_refused(self.raw_push())
        # Adopt the sibling again: installation is idempotent and durable.
        self.install_for_launch(sibling)
        gate = self.state / 'hub-prepush/gate.py'
        self.assertTrue(gate.is_file())
        suite = sibling / 'host/tests/test_fixture.py'
        suite.write_text('import unittest\nclass Fixture(unittest.TestCase):\n'
                         '    def test_result(self): self.assertTrue(False)\n')
        self.git('commit', '-qam', 'failing sibling commit', cwd=sibling)
        self.assert_refused(self.raw_push(sibling))
        suite.write_text(suite.read_text().replace('False', 'True') + '# repaired sibling\n')
        self.git('commit', '-qam', 'repair sibling commit', cwd=sibling)
        result = self.raw_push(sibling)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tip(), self.git('rev-parse', 'HEAD', cwd=sibling))

    def test_lock_repair_fallback_cannot_skip_hook_installation(self):
        from agent_hub_host.worktree_guard import protect_bound_worktree
        sibling = self.root / 'locked-sibling'
        self.git('worktree', 'add', '-qb', 'sibling', str(sibling))
        self.git('worktree', 'lock', '--reason', 'manual fixture lock', str(sibling))
        self.assertFalse(protect_bound_worktree('git', str(sibling), 'a' * 32, None))
        suite = sibling / 'host/tests/test_fixture.py'
        suite.write_text('import unittest\nclass Fixture(unittest.TestCase):\n'
                         '    def test_result(self): self.assertTrue(False)\n')
        self.git('commit', '-qam', 'failing locked sibling', cwd=sibling)
        self.assert_refused(self.raw_push(sibling))

    def test_install_failure_has_specific_redacted_launch_diagnostic(self):
        from agent_hub_host.worktree_guard import protect_bound_worktree
        from agent_hub_host.prepush import PrepushHookInstallError
        from agent_hub_host.dispatch_diagnostics import classify
        with patch('agent_hub_host.prepush.install', side_effect=PermissionError('/private/credential')):
            with self.assertRaises(PrepushHookInstallError) as caught:
                protect_bound_worktree('git', str(self.repo), 'a' * 32, None)
        diagnostic = classify(caught.exception)
        self.assertEqual(diagnostic['reason'], 'dispatch_worktree_refused')
        self.assertEqual(diagnostic['error_type'], 'PrepushHookInstallError')
        self.assertNotIn('/private/credential', str(diagnostic))


class VerifyEnvTests(unittest.TestCase):
    def test_verify_strips_git_hook_env_from_test_runs(self):
        from agent_hub_host import prepush
        seen = []
        root = Path(tempfile.mkdtemp())
        (root / 'scripts').mkdir()
        for name in ('capped-test-retry.sh', 'test-affected-local.sh'):
            (root / 'scripts' / name).write_text('')
        sha = 'a' * 40
        with patch.dict(os.environ, {'GIT_DIR': '/real/.git', 'GIT_INDEX_FILE': '/real/index', 'KEEP_ME': '1'}), \
                patch.object(prepush, 'git', return_value=sha), patch.object(prepush, 'clean', return_value=True), \
                patch.object(prepush.subprocess, 'call', side_effect=lambda *a, **k: seen.append(k['env']) or 0):
            prepush.verify(root, sha, 'b' * 40)
        self.assertTrue(seen)
        for env in seen:
            self.assertFalse([k for k in env if k.startswith('GIT_')])
            self.assertEqual(env['KEEP_ME'], '1')
            self.assertEqual(env['AGENT_HUB_HEAVY_TEST_PRIORITY'], 'final')


if __name__ == '__main__':
    unittest.main()
