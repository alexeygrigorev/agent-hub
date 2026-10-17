"""Synthetic fixtures for M14c explicit-argv probes.

Fake runners for gh/wrangler parsing; the only live subprocesses run git
against disposable synthetic fixture repos inside the owned private TMPDIR.
No network, no deploys, no operator configuration.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from agent_hub_host import status_probe

SHA_A = 'a' * 40
SHA_B = 'b' * 40
NOW = '2026-10-17T00:00:00.000Z'


def fake_runner(mapping):
    def run(argv, cwd, timeout_s):
        key = tuple(argv)
        if key in mapping:
            exit_code, stdout = mapping[key]
            return {'exit': exit_code, 'stdout': stdout, 'stderr': ''}
        return {'exit': 99, 'stdout': '', 'stderr': 'unexpected argv ' + json.dumps(argv)}
    return run


class TestProbeArgv(unittest.TestCase):
    def test_allowlisted_read_only_argv_passes(self):
        argv, reason = status_probe.check_argv(['git', 'merge-base', '--is-ancestor', SHA_A, 'origin/main'])
        self.assertEqual(reason, None)
        self.assertEqual(argv[0], 'git')

    def test_mutating_and_unknown_tools_refused(self):
        for argv in (['git', 'push', 'origin', 'main'], ['git', 'fetch', 'origin'],
                     ['git', 'ls-remote', 'origin'], ['curl', 'https://x'],
                     ['git'], 'notalist', [], ['git', 'bisect', 'start']):
            clean, reason = status_probe.check_argv(argv)
            self.assertIsNone(clean, argv)
            self.assertIsInstance(reason, str)

    def test_argv_secrets_masked_in_provenance(self):
        masked = status_probe.masked_argv(['gh', 'api', 'x', 'hubt_leaked_secret_token'])
        self.assertIn('hubt_leaked_secret_token', ['gh', 'api', 'x', 'hubt_leaked_secret_token'][3:])
        self.assertNotIn('hubt_leaked_secret_token', masked)
        self.assertIn('[masked]', masked)

    def test_child_env_scrubs_secrets(self):
        with unittest.mock.patch.dict(os.environ, {'HUB_TOKEN': 'hubt_x', 'GH_TOKEN': 'ghp_y', 'PATH': '/usr/bin'}):
            env = status_probe._scrubbed_env()
        self.assertNotIn('HUB_TOKEN', env)
        self.assertNotIn('GH_TOKEN', env)
        self.assertIn('PATH', env)

    def test_runner_output_redacted_and_bounded(self):
        runner = fake_runner({('git', 'rev-parse', 'HEAD'): (0, 'token hubt_leaked_secret_token here')})
        result = status_probe.run_argv(['git', 'rev-parse', 'HEAD'], '/tmp', 5, runner)
        self.assertNotIn('hubt_leaked_secret_token', result['stdout'])


class TestGitAncestry(unittest.TestCase):
    def repo_runner(self, head_sha, main_sha, age_s, ancestor_exit):
        return fake_runner({
            ('git', 'rev-parse', 'HEAD'): (0, head_sha + '\n'),
            ('git', 'rev-parse', 'origin/main'): (0, main_sha + '\n'),
            ('git', 'log', '-1', '--format=%ct', 'origin/main'): (0, str(int(__import__('time').time() - age_s)) + '\n'),
            ('git', 'merge-base', '--is-ancestor', head_sha, 'origin/main'): (ancestor_exit, ''),
        })

    def test_merged_only_with_fresh_ref(self):
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600,
                                                   self.repo_runner(SHA_A, SHA_B, 60, 0))
        self.assertEqual(evidence['ancestry'], 'merged_in_main')
        self.assertEqual(evidence['sha'], SHA_A)
        self.assertTrue(evidence['provenance']['ref_fresh'])

    def test_not_merged(self):
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600,
                                                   self.repo_runner(SHA_A, SHA_B, 60, 1))
        self.assertEqual(evidence['ancestry'], 'not_merged')

    def test_stale_ref_fails_closed_to_unknown(self):
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600,
                                                   self.repo_runner(SHA_A, SHA_B, 7200, 0))
        self.assertEqual(evidence['ancestry'], 'unknown')
        self.assertEqual(evidence['reason'], 'stale_ref_fails_closed')

    def test_missing_ref_is_unknown_not_merged(self):
        runner = fake_runner({
            ('git', 'rev-parse', 'HEAD'): (0, SHA_A + '\n'),
            ('git', 'rev-parse', 'origin/main'): (128, 'unknown revision'),
        })
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600, runner)
        self.assertEqual(evidence['ancestry'], 'unknown')


@unittest.skipIf(shutil.which('git') is None, 'git not installed')
class TestRealGitFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='status-probe-git-')
        self.addCleanup(self.tmp.cleanup)
        self.repo = str(Path(self.tmp.name) / 'repo')
        os.mkdir(self.repo)
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', HOME=self.tmp.name,
                   GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        self.env = env
        self.git('init', '-b', 'main')
        (Path(self.repo) / 'f.txt').write_text('one', encoding='utf-8')
        self.git('add', 'f.txt')
        self.git('commit', '-m', 'first')

    def git(self, *args):
        proc = subprocess.run(['git', *args], cwd=self.repo, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return proc.stdout.decode().strip()

    def test_real_merge_base_roundtrip(self):
        head = self.git('rev-parse', 'HEAD')
        self.assertRegex(head, r'^[0-9a-f]{40}$')
        evidence = status_probe.build_git_ancestry(self.repo, head, 'main', 3600)
        self.assertEqual(evidence['ancestry'], 'merged_in_main')
        self.git('checkout', '-b', 'side')
        (Path(self.repo) / 'g.txt').write_text('two', encoding='utf-8')
        self.git('add', 'g.txt')
        self.git('commit', '-m', 'side')
        side = self.git('rev-parse', 'HEAD')
        evidence = status_probe.build_git_ancestry(self.repo, side, 'main', 3600)
        self.assertEqual(evidence['ancestry'], 'not_merged')


class TestCiDeployment(unittest.TestCase):
    def test_ci_passing_for_exact_sha(self):
        runs = {'workflow_runs': [{'head_sha': SHA_A, 'conclusion': 'success', 'id': 42}]}
        evidence = status_probe.build_ci({'exit': 0, 'stdout': json.dumps(runs), 'stderr': ''}, SHA_A)
        self.assertEqual(evidence['state'], 'passing')
        self.assertIn('42', evidence['run'])

    def test_ci_ignores_runs_for_other_shas(self):
        runs = {'workflow_runs': [{'head_sha': SHA_B, 'conclusion': 'success', 'id': 7}]}
        evidence = status_probe.build_ci({'exit': 0, 'stdout': json.dumps(runs), 'stderr': ''}, SHA_A)
        self.assertEqual(evidence['state'], 'unknown')

    def test_ci_failing(self):
        runs = [{'head_sha': SHA_A, 'conclusion': 'failure', 'url': 'https://ci.test/r/9'}]
        evidence = status_probe.build_ci({'exit': 0, 'stdout': json.dumps(runs), 'stderr': ''}, SHA_A)
        self.assertEqual(evidence['state'], 'failing')

    def test_deployed_requires_active_record_naming_sha(self):
        payload = [{'id': 'dep-1', 'environment': 'production', 'state': 'active',
                    'sha': SHA_A, 'note': 'release'}]
        evidence = status_probe.build_deployment({'exit': 0, 'stdout': json.dumps(payload), 'stderr': ''}, SHA_A)
        self.assertEqual(evidence['state'], 'deployed')
        self.assertEqual(evidence['sha'], SHA_A)

    def test_ci_success_never_implies_deployed(self):
        payload = [{'id': 'dep-9', 'environment': 'production', 'state': 'active',
                    'sha': SHA_B, 'ci': 'passing'}]
        evidence = status_probe.build_deployment({'exit': 0, 'stdout': json.dumps(payload), 'stderr': ''}, SHA_A)
        self.assertEqual(evidence['state'], 'not_deployed')


class TestCollectProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='status-probe-')
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name))

    def spec(self, probes):
        return {'machine_id': 'host-p', 'observed_at': NOW, 'repo_roots': [self.root], 'probes': probes}

    def test_cwd_and_argv_gated_with_actionable_reasons(self):
        result = status_probe.collect_probes(self.spec([
            {'id': 'outside', 'kind': 'git_ancestry', 'cwd': '/etc', 'for_native_id': 'n',
             'sha': SHA_A, 'argv': ['git', 'rev-parse', 'HEAD']},
            {'id': 'evil', 'kind': 'ci', 'cwd': self.root, 'for_native_id': 'n',
             'sha': SHA_A, 'argv': ['curl', 'https://evil.test']},
            {'id': 'badsha', 'kind': 'git_ancestry', 'cwd': self.root, 'for_native_id': 'n',
             'sha': 'xyz', 'argv': ['git', 'rev-parse', 'HEAD']},
        ]))
        self.assertEqual(result['observations'], [])
        reasons = {s['id']: s['reason'] for s in result['skipped']}
        self.assertEqual(reasons, {'outside': 'cwd_outside_allowlist', 'evil': 'probe_tool_not_allowlisted',
                                   'badsha': 'sha_required'})

    def test_probe_git_evidence_matches_server_schema(self):
        runner = fake_runner({
            ('git', 'rev-parse', 'HEAD'): (0, SHA_A + '\n'),
            ('git', 'rev-parse', 'origin/main'): (0, SHA_B + '\n'),
            ('git', 'log', '-1', '--format=%ct', 'origin/main'): (0, str(int(__import__('time').time() - 10)) + '\n'),
            ('git', 'merge-base', '--is-ancestor', SHA_A, 'origin/main'): (0, ''),
        })
        result = status_probe.collect_probes(self.spec([
            {'id': 'g1', 'kind': 'git_ancestry', 'cwd': self.root, 'for_native_id': 'n1',
             'sha': SHA_A.upper(), 'ref': 'origin/main',
             'argv': ['git', 'rev-parse', 'HEAD']},
        ]), runner)
        self.assertEqual(len(result['observations']), 1)
        obs = result['observations'][0]
        self.assertEqual(obs['kind'], 'probe_git')
        self.assertEqual(obs['payload'], {'sha': SHA_A, 'ancestry': 'merged_in_main', 'ref': 'origin/main'})

    def test_ci_deploy_payloads_carry_sha_and_require_it(self):
        runner = fake_runner({
            ('gh', 'run', 'list'): (0, json.dumps([{'head_sha': SHA_A, 'conclusion': 'success', 'id': 3}])),
            ('wrangler', 'deployments', 'list'): (0, json.dumps(
                [{'id': 'd1', 'environment': 'production', 'state': 'active', 'sha': SHA_A}])),
        })
        result = status_probe.collect_probes(self.spec([
            {'id': 'c1', 'kind': 'ci', 'cwd': self.root, 'for_native_id': 'n1',
             'sha': SHA_A, 'argv': ['gh', 'run', 'list']},
            {'id': 'd1', 'kind': 'deployment', 'cwd': self.root, 'for_native_id': 'n1',
             'sha': SHA_A, 'argv': ['wrangler', 'deployments', 'list']},
            {'id': 'noci', 'kind': 'ci', 'cwd': self.root, 'for_native_id': 'n1',
             'argv': ['gh', 'run', 'list']},
        ]), runner)
        by_kind = {o['kind']: o['payload'] for o in result['observations']}
        self.assertEqual(by_kind['probe_ci'], {'state': 'passing', 'sha': SHA_A, 'run': '3'})
        self.assertEqual(by_kind['probe_deploy'],
                         {'state': 'deployed', 'sha': SHA_A, 'deployment': 'd1'})
        self.assertIn('sha_required', [s['reason'] for s in result['skipped']])


if __name__ == '__main__':
    unittest.main()
