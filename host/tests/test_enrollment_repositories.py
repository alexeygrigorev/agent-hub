import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from agent_hub_host import enrollment_repositories as r


class RepositoryEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = {'alias': 'demo', 'origin': 'https://github.com/example/demo.git'}
        self.grant = {'origin': self.repo['origin'], 'username': 'x-access-token',
                      'token': 'synthetic-repo-secret',
                      'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()}

    def request(self, method, path, payload=None):
        return {'repositories': [self.repo]} if method == 'GET' else dict(self.grant)

    def test_clone_uses_only_scoped_ephemeral_environment_and_no_ambient_helpers(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((list(map(str, argv)), dict(kwargs['env'])))
            (Path(argv[-1]) / '.git').mkdir(parents=True)
        with patch.dict(os.environ, {'GIT_TRACE_CURL': '1', 'GIT_CONFIG_COUNT': '9'}):
            paths = r.sync(self.root, request=self.request, runner=run)
        argv, env = calls[0]
        self.assertEqual(argv[:3], ['git', 'clone', '--'])
        self.assertNotIn(self.grant['token'], str(argv))
        self.assertNotIn('GIT_TRACE_CURL', env)
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], '')
        self.assertEqual(env['GIT_CONFIG_VALUE_1'], 'false')
        self.assertEqual(env['GIT_CONFIG_KEY_3'], 'http.' + self.repo['origin'] + '.extraHeader')
        self.assertTrue(env['GIT_CONFIG_VALUE_3'].startswith('Authorization: Basic '))
        self.assertEqual(paths, {'demo': str(self.root / 'workspaces/demo')})
        for p in self.root.rglob('*'):
            if p.is_file():
                self.assertNotIn(b'Authorization', p.read_bytes())
                self.assertNotIn(self.grant['token'].encode(), p.read_bytes())

    def test_existing_repository_is_verified_and_never_replaced(self):
        target = self.root / 'workspaces/demo'
        (target / '.git').mkdir(parents=True)
        requests = []
        def request(method, path, payload=None):
            requests.append(method)
            return self.request(method, path, payload)
        paths = r.sync(self.root, request=request,
                       runner=lambda *a, **kw: self.repo['origin'])
        self.assertEqual(requests, ['GET'])
        self.assertEqual(paths['demo'], str(target))
        with self.assertRaisesRegex(ValueError, 'origin differs'):
            r.sync(self.root, request=request, runner=lambda *a, **kw: 'https://github.com/other/repo.git')

    def test_rejects_untrusted_origins_aliases_and_grants_before_clone(self):
        for changes in ({'alias': '../escape'}, {'alias': 'con'},
                        {'origin': 'https://evil.test/example/demo.git'},
                        {'origin': 'https://github.com/example/...git'}):
            repo = dict(self.repo, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                r.sync(self.root, request=lambda *a: {'repositories': [repo]},
                       runner=lambda *a, **kw: self.fail('must not run git'))
        for changes in ({'username': 'other'}, {'origin': 'https://github.com/other/repo.git'},
                        {'expires_at': 'not-a-time'}, {'token': 'bad\nvalue'}):
            self.grant.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                r.sync(self.root, request=self.request,
                       runner=lambda *a, **kw: self.fail('must not clone'))

    def test_missing_policy_is_explicit_but_service_outage_is_not_hidden(self):
        def request(error):
            def call(*args):
                raise urllib.error.HTTPError('https://hub.test/api/host/repositories', 503,
                    'Service unavailable', {}, io.BytesIO(json.dumps({'error': error}).encode()))
            return call
        self.assertIsNone(r.sync(self.root, request=request('repository_access_not_configured')))
        with self.assertRaises(urllib.error.HTTPError):
            r.sync(self.root, request=request('repository_access_unavailable'))

    def test_failed_clone_does_not_publish_a_partial_repository_and_can_resume(self):
        def fail(argv, **kwargs):
            (Path(argv[-1]) / '.git').mkdir(parents=True)
            raise RuntimeError('synthetic interrupted clone')
        with self.assertRaises(RuntimeError):
            r.sync(self.root, request=self.request, runner=fail)
        self.assertEqual(list((self.root / 'workspaces').iterdir()), [])
        def succeed(argv, **kwargs):
            (Path(argv[-1]) / '.git').mkdir(parents=True)
        r.sync(self.root, request=self.request, runner=succeed)
        self.assertTrue((self.root / 'workspaces/demo/.git').is_dir())

    def stage_residue(self, *, owned):
        digest = hashlib.sha256((self.repo['alias'] + '\0' + self.repo['origin']).encode()).hexdigest()
        stage = self.root / 'workspaces' / ('.clone-owned-' + digest)
        stage.mkdir(parents=True, mode=0o700)
        stage.parent.chmod(0o700)
        (stage / 'partial').write_text('interrupted clone')
        if owned:
            from agent_hub_host import private_fs
            marker = stage.with_name(stage.name + '.json')
            private_fs.atomic_write(marker, json.dumps(dict(self.repo, version=1)).encode(), 4096)
        return stage

    def test_sigkill_residue_is_recovered_only_with_matching_ownership(self):
        stage = self.stage_residue(owned=True)
        unknown = stage.parent / '.clone-other-session'
        unknown.mkdir()
        (unknown / 'keep').write_text('other owner')
        def succeed(argv, **kwargs):
            self.assertFalse((stage / 'partial').exists())
            self.assertEqual(kwargs['timeout'], 1800)
            (Path(argv[-1]) / '.git').mkdir(parents=True)
        r.sync(self.root, request=self.request, runner=succeed)
        self.assertFalse(stage.exists())
        self.assertEqual((unknown / 'keep').read_text(), 'other owner')

    def test_unmarked_stage_is_preserved(self):
        stage = self.stage_residue(owned=False)
        with self.assertRaisesRegex(ValueError, 'Unowned'):
            r.sync(self.root, request=self.request, runner=lambda *a, **kw: self.fail('no clone'))
        self.assertEqual((stage / 'partial').read_text(), 'interrupted clone')

    def test_live_clone_lock_prevents_cleanup_or_duplicate_clone(self):
        stage = self.stage_residue(owned=True)
        digest = stage.name.removeprefix('.clone-owned-')
        from agent_hub_host import private_fs
        with private_fs.exclusive_lock(self.root / 'locks' / ('repository-' + digest + '.lock')):
            with self.assertRaises(OSError):
                r.sync(self.root, request=self.request, runner=lambda *a, **kw: self.fail('no clone'))
        self.assertEqual((stage / 'partial').read_text(), 'interrupted clone')


if __name__ == '__main__':
    unittest.main()
