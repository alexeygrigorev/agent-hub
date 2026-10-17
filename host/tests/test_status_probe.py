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
                     ['git', 'ls-remote', 'origin'], ['git', 'ls-remote', 'origin', 'refs/heads/main', 'extra'],
                     ['git', 'ls-remote', 'evil', 'refs/heads/main'],
                     ['curl', 'https://x'],
                     ['git'], 'notalist', [], ['git', 'bisect', 'start'],
                     ['gh', 'api', 'repos/x', '--method', 'POST'],
                     ['gh', 'api', 'repos/x', '-X', 'POST'],
                     ['gh', 'api', 'repos/x', 'POST'],
                     ['gh', 'run', 'cancel', '1'],
                     ['gh', 'run', 'rerun', '1'],
                     ['wrangler', 'deployments', 'delete']):
            clean, reason = status_probe.check_argv(argv)
            self.assertIsNone(clean, argv)
            self.assertIsInstance(reason, str)

    def test_readonly_ls_remote_and_gh_list_pass(self):
        argv, reason = status_probe.check_argv(['git', 'ls-remote', 'origin', 'refs/heads/main'])
        self.assertIsNotNone(argv)
        self.assertIsNone(reason)
        for argv_ok in (['gh', 'api', 'repos/o/r/actions/runs'],
                        ['gh', 'run', 'list', '--limit', '5'],
                        ['wrangler', 'deployments', 'list']):
            clean, reason = status_probe.check_argv(argv_ok)
            self.assertIsNotNone(clean, argv_ok)
            self.assertIsNone(reason, argv_ok)

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
    def repo_runner(self, head_sha, local_sha, remote_sha, ancestor_exit, remote_exit=0):
        mapping = {
            ('git', 'rev-parse', 'HEAD'): (0, head_sha + '\n'),
            ('git', 'rev-parse', 'origin/main'): (0, local_sha + '\n'),
            ('git', 'ls-remote', 'origin', 'refs/heads/main'): (remote_exit, (remote_sha + '\trefs/heads/main\n') if remote_sha else ''),
            ('git', 'merge-base', '--is-ancestor', head_sha, 'origin/main'): (ancestor_exit, ''),
        }
        # merge-base is called with the probe sha, not head_sha; mirror both.
        mapping[('git', 'merge-base', '--is-ancestor', SHA_A, 'origin/main')] = (ancestor_exit, '')
        return fake_runner(mapping)

    def test_merged_only_with_fresh_ref(self):
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600,
                                                   self.repo_runner(SHA_A, SHA_B, SHA_B, 0))
        self.assertEqual(evidence['ancestry'], 'merged_in_main')
        self.assertEqual(evidence['sha'], SHA_A)
        self.assertTrue(evidence['provenance']['ref_fresh'])

    def test_old_commit_with_current_remote_stays_fresh(self):
        # Commit-time proxy bug: an old commit on a perfectly current remote
        # must NOT go stale. Freshness is exact SHA equality.
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 60,
                                                   self.repo_runner(SHA_A, SHA_B, SHA_B, 0))
        self.assertEqual(evidence['ancestry'], 'merged_in_main')

    def test_not_merged(self):
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600,
                                                   self.repo_runner(SHA_A, SHA_B, SHA_B, 1))
        self.assertEqual(evidence['ancestry'], 'not_merged')

    def test_stale_ref_fails_closed_to_unknown(self):
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600,
                                                   self.repo_runner(SHA_A, SHA_B, SHA_A, 0))
        self.assertEqual(evidence['ancestry'], 'unknown')
        self.assertEqual(evidence['reason'], 'stale_ref_fails_closed')

    def test_network_unknown_fails_closed(self):
        evidence = status_probe.build_git_ancestry('/repo', SHA_A, 'origin/main', 3600,
                                                   self.repo_runner(SHA_A, SHA_B, '', 0, remote_exit=128))
        self.assertEqual(evidence['ancestry'], 'unknown')
        self.assertEqual(evidence['reason'], 'network_unknown_fails_closed')

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
        # Local bare origin so `git ls-remote origin refs/heads/main` is a
        # real read-only fresh-remote read without network.
        bare = str(Path(self.tmp.name) / 'origin.git')
        proc = subprocess.run(['git', 'init', '--bare', bare], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.git('remote', 'add', 'origin', bare)
        self.git('push', '-u', 'origin', 'main')
        head = self.git('rev-parse', 'HEAD')
        self.assertRegex(head, r'^[0-9a-f]{40}$')
        evidence = status_probe.build_git_ancestry(self.repo, head, 'origin/main', 3600)
        self.assertEqual(evidence['ancestry'], 'merged_in_main')
        self.git('checkout', '-b', 'side')
        (Path(self.repo) / 'g.txt').write_text('two', encoding='utf-8')
        self.git('add', 'g.txt')
        self.git('commit', '-m', 'side')
        side = self.git('rev-parse', 'HEAD')
        evidence = status_probe.build_git_ancestry(self.repo, side, 'origin/main', 3600)
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

    def test_unrelated_note_or_history_containing_target_sha_does_not_count(self):
        # Regression test: historical note/description mentioning target SHA
        # must NOT cause loose matching when another commit is actually active.
        payload = [{'id': 'dep-2', 'environment': 'production', 'state': 'active',
                    'sha': SHA_B, 'note': f'Revert of commit {SHA_A}',
                    'description': f'Fixes bug introduced by {SHA_A}'}]
        evidence = status_probe.build_deployment({'exit': 0, 'stdout': json.dumps(payload), 'stderr': ''}, SHA_A)
        self.assertEqual(evidence['state'], 'not_deployed')
        self.assertEqual(evidence['reason'], 'no_active_deployment_names_sha')


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
            ('git', 'ls-remote', 'origin', 'refs/heads/main'): (0, SHA_B + '\trefs/heads/main\n'),
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


class TestCloudflareCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cf-creds-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_cred_file(self, content: str, mode: int = 0o600) -> str:
        path = self.root / 'creds.env'
        path.write_text(content, encoding='utf-8')
        os.chmod(path, mode)
        return str(path)

    def test_valid_0600_credentials_file_succeeds(self):
        path = self.write_cred_file(
            '# Cloudflare credentials\n'
            'CLOUDFLARE_ACCOUNT_ID=0123456789abcdef0123456789abcdef\n'
            'CLOUDFLARE_API_TOKEN=cf-secret-token-12345678\n'
            'OTHER_SECRET=do_not_load_this\n'
        )
        creds, err = status_probe.load_cloudflare_credentials(path)
        self.assertIsNone(err)
        self.assertEqual(creds, {
            'account_id': '0123456789abcdef0123456789abcdef',
            'api_token': 'cf-secret-token-12345678'
        })
        self.assertNotIn('OTHER_SECRET', creds)

    def test_symlink_refused(self):
        target = self.write_cred_file(
            'CLOUDFLARE_ACCOUNT_ID=abc\nCLOUDFLARE_API_TOKEN=cf-secret-token-12345678\n'
        )
        symlink_path = self.root / 'symlink.env'
        os.symlink(target, symlink_path)
        creds, err = status_probe.load_cloudflare_credentials(str(symlink_path))
        self.assertIsNone(creds)
        self.assertEqual(err, 'credentials_file_symlink_refused')

    def test_broad_permissions_refused(self):
        for mode in (0o644, 0o660, 0o664, 0o777, 0o604):
            path = self.write_cred_file(
                'CLOUDFLARE_ACCOUNT_ID=abc\nCLOUDFLARE_API_TOKEN=cf-secret-token-12345678\n',
                mode=mode
            )
            creds, err = status_probe.load_cloudflare_credentials(path)
            self.assertIsNone(creds, f"mode {oct(mode)} should have been refused")
            self.assertEqual(err, 'credentials_file_broad_permissions')

    def test_wrong_owner_refused(self):
        path = self.write_cred_file(
            'CLOUDFLARE_ACCOUNT_ID=abc\nCLOUDFLARE_API_TOKEN=cf-secret-token-12345678\n'
        )
        fake_st = os.lstat(path)
        with unittest.mock.patch('os.lstat') as mock_lstat:
            mock_stat_res = unittest.mock.MagicMock()
            mock_stat_res.st_mode = fake_st.st_mode
            mock_stat_res.st_uid = os.getuid() + 999
            mock_lstat.return_value = mock_stat_res
            creds, err = status_probe.load_cloudflare_credentials(path)
            self.assertIsNone(creds)
            self.assertEqual(err, 'credentials_file_wrong_owner')

    def test_non_regular_file_refused(self):
        dir_path = self.root / 'dir_creds'
        dir_path.mkdir()
        creds, err = status_probe.load_cloudflare_credentials(str(dir_path))
        self.assertIsNone(creds)
        self.assertEqual(err, 'credentials_file_not_regular')

    def test_missing_file_refused(self):
        creds, err = status_probe.load_cloudflare_credentials(str(self.root / 'nonexistent.env'))
        self.assertIsNone(creds)
        self.assertEqual(err, 'credentials_file_not_found')

    def test_missing_fields_refused(self):
        path1 = self.write_cred_file('CLOUDFLARE_API_TOKEN=cf-secret-token-12345678\n')
        creds, err = status_probe.load_cloudflare_credentials(path1)
        self.assertIsNone(creds)
        self.assertEqual(err, 'credentials_missing_account_id')

        path2 = self.write_cred_file('CLOUDFLARE_ACCOUNT_ID=0123456789abcdef\n')
        creds, err = status_probe.load_cloudflare_credentials(path2)
        self.assertIsNone(creds)
        self.assertEqual(err, 'credentials_missing_api_token')

    def test_file_too_large_refused(self):
        oversized = 'CLOUDFLARE_ACCOUNT_ID=0123456789abcdef\n' + ('# pad\n' * 15000)
        path = self.write_cred_file(oversized)
        creds, err = status_probe.load_cloudflare_credentials(path)
        self.assertIsNone(creds)
        self.assertEqual(err, 'credentials_file_too_large')


VERSION_UUID_1 = '11111111-2222-3333-4444-555555555555'
VERSION_UUID_2 = '66666666-7777-8888-9999-000000000000'


class TestCloudflareWorkerDeploymentAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cf-deploy-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cred_path = str(self.root / 'creds.env')
        Path(self.cred_path).write_text(
            'CLOUDFLARE_ACCOUNT_ID=acc123\nCLOUDFLARE_API_TOKEN=cf-token-12345678\n',
            encoding='utf-8'
        )
        os.chmod(self.cred_path, 0o600)

    def spec(self, probes):
        return {
            'machine_id': 'host-cf',
            'observed_at': NOW,
            'repo_roots': [str(self.root)],
            'probes': probes
        }

    def make_transport(self, deployments, versions_map=None, status_code=200, error=None):
        versions_map = versions_map or {}
        call_log = []

        def transport(url, headers, timeout_s):
            call_log.append({'url': url, 'headers': headers, 'timeout_s': timeout_s})
            if error is not None:
                raise error
            if status_code != 200:
                return {'status': status_code, 'body': json.dumps({'success': False, 'errors': [{'code': status_code}]})}
            if '/deployments' in url:
                return {'status': 200, 'body': json.dumps({'success': True, 'result': deployments})}
            for vid, v_data in versions_map.items():
                if f'/versions/{vid}' in url:
                    return {'status': 200, 'body': json.dumps({'success': True, 'result': v_data})}
            return {'status': 404, 'body': json.dumps({'success': False, 'errors': ['not_found']})}

        return transport, call_log

    def test_exact_active_sha_passes(self):
        deployments = [{
            'id': 'dep-active-1',
            'versions': [{'version_id': VERSION_UUID_1, 'percentage': 100}]
        }]
        versions_map = {
            VERSION_UUID_1: {
                'annotations': {'workers/message': f'Release source_commit={SHA_A}'}
            }
        }
        transport, calls = self.make_transport(deployments, versions_map)
        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe]), http_transport=transport)

        self.assertEqual(len(result['observations']), 1)
        obs = result['observations'][0]
        self.assertEqual(obs['kind'], 'probe_deploy')
        self.assertEqual(obs['payload']['state'], 'deployed')
        self.assertEqual(obs['payload']['sha'], SHA_A)
        self.assertIn(VERSION_UUID_1, obs['payload']['deployment'])
        self.assertIn('100% traffic', obs['payload']['deployment'])

    def test_old_active_new_historical_fails(self):
        deployments = [{
            'id': 'dep-active-2',
            'versions': [{'version_id': VERSION_UUID_1, 'percentage': 100}]
        }]
        versions_map = {
            VERSION_UUID_1: {
                'annotations': {'workers/message': f'Release source_commit={SHA_B}'}
            }
        }
        transport, _ = self.make_transport(deployments, versions_map)
        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe]), http_transport=transport)

        self.assertEqual(len(result['observations']), 1)
        obs = result['observations'][0]
        self.assertEqual(obs['payload']['state'], 'not_deployed')
        self.assertEqual(obs['payload']['sha'], SHA_A)

    def test_wrong_or_missing_annotations_yields_unknown(self):
        deployments = [{
            'id': 'dep-active-3',
            'versions': [{'version_id': VERSION_UUID_1, 'percentage': 100}]
        }]
        # Arbitrary tag or text note without source_commit=40hex does NOT prove source
        versions_map = {
            VERSION_UUID_1: {
                'annotations': {
                    'workers/message': f'Generic message mentioning {SHA_A}',
                    'workers/tag': 'v1.2.3'
                }
            }
        }
        transport, _ = self.make_transport(deployments, versions_map)
        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe]), http_transport=transport)

        self.assertEqual(len(result['observations']), 1)
        obs = result['observations'][0]
        self.assertEqual(obs['payload']['state'], 'unknown')

    def test_mixed_canary_traffic_yields_unknown(self):
        deployments = [{
            'id': 'dep-canary',
            'versions': [
                {'version_id': VERSION_UUID_1, 'percentage': 90},
                {'version_id': VERSION_UUID_2, 'percentage': 10}
            ]
        }]
        versions_map = {
            VERSION_UUID_1: {'annotations': {'workers/message': f'source_commit={SHA_A}'}},
            VERSION_UUID_2: {'annotations': {'workers/message': f'source_commit={SHA_B}'}},
        }
        transport, _ = self.make_transport(deployments, versions_map)
        probe1 = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        probe2 = status_probe.make_cloudflare_probe('p2', 'n2', SHA_B, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe1, probe2]), http_transport=transport)

        states = [o['payload']['state'] for o in result['observations']]
        self.assertEqual(states, ['unknown', 'unknown'])

    def test_empty_or_missing_data_yields_unknown(self):
        # Empty deployments list
        transport, _ = self.make_transport([])
        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe]), http_transport=transport)
        self.assertEqual(result['observations'][0]['payload']['state'], 'unknown')

        # Invalid version id
        dep_bad_vid = [{'id': 'd', 'versions': [{'version_id': 'bad-id', 'percentage': 100}]}]
        transport2, _ = self.make_transport(dep_bad_vid)
        result2 = status_probe.collect_probes(self.spec([probe]), http_transport=transport2)
        self.assertEqual(result2['observations'][0]['payload']['state'], 'unknown')

        # Invalid percentage sum
        dep_bad_pct = [{'id': 'd', 'versions': [{'version_id': VERSION_UUID_1, 'percentage': 50}]}]
        transport3, _ = self.make_transport(dep_bad_pct)
        result3 = status_probe.collect_probes(self.spec([probe]), http_transport=transport3)
        self.assertEqual(result3['observations'][0]['payload']['state'], 'unknown')

    def test_http_errors_yield_unknown(self):
        for status_code in (401, 429, 500):
            transport, _ = self.make_transport([], status_code=status_code)
            probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
            result = status_probe.collect_probes(self.spec([probe]), http_transport=transport)
            self.assertEqual(result['observations'][0]['payload']['state'], 'unknown',
                             f"status {status_code} should be unknown")

        # Timeout
        transport_timeout, _ = self.make_transport([], error=TimeoutError('timed out'))
        result_to = status_probe.collect_probes(self.spec([probe]), http_transport=transport_timeout)
        self.assertEqual(result_to['observations'][0]['payload']['state'], 'unknown')

    def test_malformed_json_and_response_bounds(self):
        def bad_json_transport(url, headers, timeout_s):
            return {'status': 200, 'body': '<html>error</html>'}

        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe]), http_transport=bad_json_transport)
        self.assertEqual(result['observations'][0]['payload']['state'], 'unknown')

        def oversized_transport(url, headers, timeout_s):
            return {'status': 200, 'body': ' ' * (status_probe.MAX_CF_BODY + 10)}

        result_ov = status_probe.collect_probes(self.spec([probe]), http_transport=oversized_transport)
        self.assertEqual(result_ov['observations'][0]['payload']['state'], 'unknown')

    def test_redirect_refusal(self):
        def redirect_transport(url, headers, timeout_s):
            return {'status': 302, 'body': ''}

        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe]), http_transport=redirect_transport)
        self.assertEqual(result['observations'][0]['payload']['state'], 'unknown')

    def test_no_secret_in_provenance_or_payload(self):
        deployments = [{
            'id': 'dep-active-1',
            'versions': [{'version_id': VERSION_UUID_1, 'percentage': 100}]
        }]
        versions_map = {
            VERSION_UUID_1: {
                'annotations': {'workers/message': f'Release source_commit={SHA_A}'}
            }
        }
        transport, _ = self.make_transport(deployments, versions_map)
        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe]), http_transport=transport)

        obs = result['observations'][0]
        # Payload keys must strictly adhere to Hub contract: state, sha, deployment
        self.assertTrue(set(obs['payload'].keys()).issubset({'state', 'sha', 'deployment'}))
        dumped = json.dumps(obs)
        self.assertNotIn('cf-token-12345678', dumped)
        self.assertNotIn(self.cred_path, dumped)

    def test_snapshot_reuse_within_collection_call(self):
        deployments = [{
            'id': 'dep-active-1',
            'versions': [{'version_id': VERSION_UUID_1, 'percentage': 100}]
        }]
        versions_map = {
            VERSION_UUID_1: {
                'annotations': {'workers/message': f'Release source_commit={SHA_A}'}
            }
        }
        transport, call_log = self.make_transport(deployments, versions_map)
        # Two probes for different sessions and different SHAs targeting the same script
        probe1 = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, self.cred_path)
        probe2 = status_probe.make_cloudflare_probe('p2', 'n2', SHA_B, self.cred_path)
        result = status_probe.collect_probes(self.spec([probe1, probe2]), http_transport=transport)

        self.assertEqual(len(result['observations']), 2)
        self.assertEqual(result['observations'][0]['payload']['state'], 'deployed')
        self.assertEqual(result['observations'][1]['payload']['state'], 'not_deployed')
        # Deployments was called only once, version detail was called only once!
        self.assertEqual(len(call_log), 2)

    def test_credentials_failure_yields_unknown_deploy_observation(self):
        # 0777 credentials file mode fails security check
        bad_cred_path = str(self.root / 'bad_creds.env')
        Path(bad_cred_path).write_text(
            'CLOUDFLARE_ACCOUNT_ID=acc\nCLOUDFLARE_API_TOKEN=cf-token-12345678\n',
            encoding='utf-8'
        )
        os.chmod(bad_cred_path, 0o777)
        probe = status_probe.make_cloudflare_probe('p1', 'n1', SHA_A, bad_cred_path)
        result = status_probe.collect_probes(self.spec([probe]))

        self.assertEqual(len(result['observations']), 1)
        obs = result['observations'][0]
        self.assertEqual(obs['kind'], 'probe_deploy')
        self.assertEqual(obs['payload']['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()

