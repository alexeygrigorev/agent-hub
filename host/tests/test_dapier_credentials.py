"""Real CLI process isolation and renewal failures never expose credentials."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from agent_hub_host import dapier_credentials as d


class DapierCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'shared'
        self.now = int(time.time())
        self.calls = []

    def seed(self, **updates):
        session = dict(email=d.IDENTITY, kind='device', token='dapd_fake_credential',
                       expires_at=self.now + 30 * 86400)
        session.update(updates)
        d.private_write(self.root / 'dapier/session.json', json.dumps(session))
        return session

    def maintain(self, **kwargs):
        return d.maintain(self.root, self.now, notifier=lambda *args: self.calls.append(args), **kwargs)

    def test_does_not_copy_operator_credential_and_alerts_missing_seed(self):
        self.assertEqual(self.maintain(), 'dapier_pairing_required')
        self.assertFalse((self.root / 'dapier/session.json').exists())
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('token', (self.root / 'dapier-health.json').read_text())
        self.maintain()
        self.assertEqual(len(self.calls), 1)

    def test_rejects_wrong_identity_and_expired_session_without_refresh(self):
        for updates, reason in [({'email': 'other@example.test'}, 'dapier_identity_mismatch'),
                                ({'expires_at': self.now - 1}, 'dapier_pairing_required')]:
            with self.subTest(reason=reason):
                self.seed(**updates)
                (self.root / 'dapier-health.json').unlink(missing_ok=True)
                refresh = mock.Mock()
                self.assertEqual(self.maintain(refresher=refresh), reason)
                refresh.assert_not_called()

    def test_proactive_rotation_is_private_and_shared(self):
        self.seed(expires_at=self.now + 86400)
        refresh = mock.Mock(return_value={'token': 'dapd_rotated_fake', 'expires_at': self.now + 30 * 86400})
        self.assertEqual(self.maintain(refresher=refresh), 'rotated')
        refresh.assert_called_once_with('dapd_fake_credential')
        session = d.read_session(self.root, self.now)
        self.assertEqual(session['token'], 'dapd_rotated_fake')
        self.assertEqual((self.root / 'dapier/session.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.calls, [])

    def test_rejected_refresh_alerts_developer_without_remote_body(self):
        self.seed(expires_at=self.now + 86400)
        refresh = mock.Mock(side_effect=ValueError('dapier_refresh_rejected'))
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual(self.maintain(refresher=refresh), 'dapier_refresh_rejected')
        self.assertIn('recipient=Developer', logs.output[0])
        self.assertNotIn('dapd_', str(logs.output) + str(self.calls))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(d.read_session(self.root, self.now)['token'], 'dapd_fake_credential')

    def test_failed_alert_is_retried_with_same_episode(self):
        episodes = []
        def fail(root, reason, episode):
            episodes.append(episode)
            raise RuntimeError('private remote response')
        d.maintain(self.root, self.now, notifier=fail)
        d.maintain(self.root, self.now + d.CHECK_INTERVAL, notifier=fail)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0], episodes[1])
        self.assertFalse(json.loads((self.root / 'dapier-health.json').read_text())['notified'])

    def test_http_401_has_only_a_fixed_error(self):
        import urllib.error
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(d.ORIGIN, 401, 'secret remote body', {}, None)
        with mock.patch.object(d.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, '^dapier_refresh_rejected$'):
                d.rotate('dapd_private_fake')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, d.ORIGIN + '/api/agent/device/refresh')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)
        self.assertIsNone(d.NoRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://other.test'))

    def test_alert_uses_operator_profile_and_never_credential_arguments(self):
        with mock.patch.dict(os.environ, {'HUB_TOKEN': 'private_fake', 'HUB_JOB_TOKEN': 'private_job'}), \
                mock.patch.object(d.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            d.notify(self.root, 'dapier_refresh_rejected', 'episode')
        args, env = run.call_args.args[0], run.call_args.kwargs['env']
        self.assertEqual(args[args.index('--to') + 1], 'agent:dev')
        self.assertNotIn('private_fake', str(args))
        self.assertNotIn('HUB_TOKEN', env)
        self.assertNotIn('HUB_JOB_TOKEN', env)
        self.assertEqual(env['HUB_CONFIG_FILE'], str(self.root.parent / 'cli.json'))

    def test_concurrent_check_cannot_fork_a_refresh(self):
        import fcntl
        self.seed(expires_at=self.now + 86400)
        with open(self.root / '.dapier.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            refresh = mock.Mock()
            self.assertEqual(self.maintain(refresher=refresh), 'busy')
            refresh.assert_not_called()

    def test_far_from_expiry_does_not_rotate(self):
        self.seed()
        refresh = mock.Mock()
        self.assertEqual(self.maintain(refresher=refresh), 'valid')
        refresh.assert_not_called()

    def test_real_process_uses_shared_identity_despite_expired_host_store(self):
        self.seed()
        home = Path(self.temp.name) / 'host'
        d.private_write(home / '.config/dapier/session.json', json.dumps({'email': 'wrong@example.test', 'expires_at': 0}))
        binary = Path(self.temp.name) / 'bin/dapier'
        binary.parent.mkdir()
        binary.write_text('#!/usr/bin/env python3\nimport json, os\nfrom pathlib import Path\ns=json.loads((Path(os.environ["XDG_CONFIG_HOME"])/"dapier/session.json").read_text())\nprint(s["email"])\n')
        binary.chmod(0o700)
        env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'))
        env.update(d.session_environment(self.root, str(binary.parent) + os.pathsep + os.environ['PATH']))
        result = subprocess.run(['dapier', 'auth', 'status'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), d.IDENTITY)
        self.seed(email='wrong@example.test')
        result = subprocess.run(['dapier', 'auth', 'status'], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Developer', result.stderr)
        self.assertNotIn('dapd_', result.stderr)

    def test_package_sources_stay_identical(self):
        repo = Path(__file__).resolve().parents[2]
        self.assertEqual((repo / 'host/agent_hub_host/dapier_credentials.py').read_bytes(),
                         (repo / 'hub2/host/agent_hub_host/dapier_credentials.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
