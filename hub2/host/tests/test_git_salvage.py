import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host.git_salvage import salvage_branch
import test_host


class GitSalvageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ceiling = patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.root)})
        ceiling.start()
        self.addCleanup(ceiling.stop)
        self.repo, self.remote = self.root / 'work', self.root / 'origin.git'
        self.repo.mkdir()
        self.git('init', '--initial-branch=trunk')
        self.git('config', 'user.name', 'Salvage test')
        self.git('config', 'user.email', 'salvage@example.invalid')
        self.commit('base')
        self.git('init', '--bare', '--initial-branch=trunk', str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'trunk')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-b', 'task/work')
        self.sha = self.commit('work')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self, text):
        (self.repo / 'file').write_text(text)
        self.git('add', 'file')
        self.git('commit', '-m', text)
        return self.git('rev-parse', 'HEAD')

    def test_pushes_commits_preserves_dirty_files_and_reports_exact_tip(self):
        (self.repo / 'dirty').write_text('uncommitted')
        result = salvage_branch(self.repo)
        self.assertEqual(result, {'repo': str(self.repo), 'remote': 'origin', 'branch': 'task/work',
                                 'head_sha': self.sha, 'status': 'pushed'})
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/task/work').split()[0], self.sha)
        self.assertEqual((self.repo / 'dirty').read_text(), 'uncommitted')
        self.assertEqual(salvage_branch(self.repo)['status'], 'already_present')

    def test_never_pushes_custom_default_branch(self):
        self.git('checkout', 'trunk')
        self.commit('default local only')
        self.assertIsNone(salvage_branch(self.repo))
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/trunk').split()[0], self.base)

    def test_diverged_branch_is_never_forced_even_with_force_configuration(self):
        self.git('push', 'origin', 'task/work')
        self.git('reset', '--hard', self.base)  # disposable fixture only
        local = self.commit('diverged')
        self.git('config', 'remote.origin.push', '+refs/heads/*:refs/heads/*')
        self.git('config', 'push.followTags', 'true')
        result = salvage_branch(self.repo)
        self.assertEqual((result['status'], result['reason']), ('failed', 'push_rejected'))
        self.assertEqual(result['head_sha'], local)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/task/work').split()[0], self.sha)

    def test_no_remote_detached_and_non_repository_are_silent(self):
        self.git('remote', 'remove', 'origin')
        self.assertIsNone(salvage_branch(self.repo))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('checkout', '--detach')
        self.assertIsNone(salvage_branch(self.repo))
        outside = self.root / 'no-repository'
        outside.mkdir()
        self.assertIsNone(salvage_branch(outside))

    def test_unknown_default_fails_closed(self):
        subprocess.check_call(['git', '--git-dir', str(self.remote), 'symbolic-ref', 'HEAD', 'refs/heads/missing'])
        self.assertEqual(salvage_branch(self.repo)['reason'], 'default_branch_unknown')
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/task/work'), '')

    def test_pushurl_default_is_checked_instead_of_fetch_default(self):
        other = self.root / 'push.git'
        self.git('init', '--bare', '--initial-branch=task/work', str(other))
        self.git('push', str(other), 'task/work')
        self.git('config', 'remote.origin.pushurl', str(other))
        self.commit('must stay local')
        self.assertIsNone(salvage_branch(self.repo))
        self.assertEqual(self.git('ls-remote', str(other), 'refs/heads/task/work').split()[0], self.sha)

    def test_multiple_push_destinations_fail_closed(self):
        self.git('config', '--add', 'remote.origin.pushurl', str(self.remote))
        self.git('config', '--add', 'remote.origin.pushurl', str(self.root / 'other.git'))
        self.assertEqual(salvage_branch(self.repo)['reason'], 'ambiguous_push_destination')

    def test_timeout_reports_failure_without_leaking_git_output(self):
        run = subprocess.run
        def slow(args, **kwargs):
            if 'push' in args:
                raise subprocess.TimeoutExpired(args, 1, stderr='private-token')
            return run(args, **kwargs)
        with patch('agent_hub_host.git_salvage.subprocess.run', side_effect=slow):
            result = salvage_branch(self.repo)
        self.assertEqual(result['reason'], 'git_unavailable_or_timeout')
        self.assertNotIn('private-token', json.dumps(result))


class SalvageExitTests(unittest.TestCase):
    def test_every_verified_exit_journals_salvage_before_finish_and_only_once(self):
        for ending in ('normal', 'deadline', 'stop'):
            with self.subTest(ending=ending):
                fixture = test_host.HostTests()
                fixture.setUp()
                try:
                    fixture.bridge.offer(fixture.offer())
                    if ending == 'deadline':
                        with fixture.journal.db:
                            fixture.journal.db.execute('UPDATE launches SET runtime_deadline=?', (int(time.time()*1000)-1,))
                    elif ending == 'stop':
                        fixture.native(['aplexer', 'kill', fixture.native.process['id']])
                    else:
                        fixture.native.process.update(phase='exited', containment_empty=True, exit={'code': 0})
                    proof = {'repo': '/repo', 'branch': 'task/saved', 'head_sha': 'a'*40, 'status': 'pushed'}
                    with patch('agent_hub_host.agent.salvage_branch', return_value=proof) as salvage:
                        fixture.bridge.reconcile()
                        fixture.bridge.reconcile()
                        salvage.assert_called_once_with(str(fixture.root))
                    payload = json.loads(fixture.journal.db.execute("SELECT payload FROM outbox WHERE path LIKE '%/finish'").fetchone()['payload'])
                    self.assertEqual(payload['evidence']['git_salvage'], proof)
                finally:
                    fixture.doCleanups()
                    fixture.tearDown()
