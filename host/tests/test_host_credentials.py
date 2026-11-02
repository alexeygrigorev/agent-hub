"""Hub-owned gh token / shared git SSH key / identity on the host (intake 54d5b703)."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub_host import host_credentials as hc

TOKEN = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0' * 2
PRIV = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE=\n-----END OPENSSH PRIVATE KEY-----\n'
PUB = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl agent-hub'
STATUS_OUT = ("github.com\n  ✓ Logged in to github.com account alexeygrigorev (keyring)\n  - Active account: true\n"
              "  - Git operations protocol: ssh\n  - Token: gho_****\n  - Token scopes: 'gist', 'read:org', 'repo'\n")


def done(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


class FakeRunner:
    """Records argv and stdin; plays gh login state in memory."""

    def __init__(self, logged_in=False):
        self.calls = []
        self.logged_in = logged_in
        self.git = {}

    def __call__(self, argv, input=None, **kw):
        self.calls.append((list(argv), input, kw.get('env')))
        name = Path(argv[0]).name
        if name.startswith('gh'):
            if argv[1:3] == ['auth', 'status']:
                return done(0, STATUS_OUT) if self.logged_in else done(1, '', 'You are not logged into any GitHub hosts.')
            if argv[1:3] == ['auth', 'login']:
                self.logged_in = input is not None and input.strip().startswith('ghp_')
                return done(0 if self.logged_in else 1)
            if argv[1] == 'api':
                return done(0, 'ecdsa-sha2-nistp256 ' + 'A' * 60 + '\n')
            return done(0)
        if name.startswith('git'):
            if '--get' in argv:
                return done(0 if argv[-1] in self.git else 1, self.git.get(argv[-1], '') + '\n')
            self.git[argv[-2]] = argv[-1]
            return done(0)
        if name.startswith('ssh-keygen'):
            return done(0, '256 SHA256:' + 'a' * 43 + ' agent-hub (ED25519)\n')
        if name.startswith('ssh'):
            return done(1, '', "Hi alexeygrigorev! You've successfully authenticated, but GitHub does not provide shell access.\n")
        return done(0)


class HostCredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ssh = Path(self.tmp.name) / 'ssh'
        self.managed = Path(self.tmp.name) / 'managed'
        patches = [mock.patch.dict(os.environ, {'AGENT_HUB_SSH_DIR': str(self.ssh), 'AGENT_HUB_MANAGED_DIR': str(self.managed)}),
                   mock.patch.object(hc, 'find_gh', return_value='/usr/bin/gh'),
                   mock.patch.object(hc.shutil, 'which', side_effect=lambda n: '/usr/bin/' + n)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        hc._last.update(poll=0.0, status=0.0, body=None)

    def hub(self, payloads, gens=None, resync=False):
        calls = []

        def request(method, path, body=None):
            calls.append((method, path, body))
            if path.startswith('/api/host-credentials/sync'):
                return {'generations': gens or {k: 1 for k in payloads}, 'forced': False,
                        'credentials': {k: {'generation': 1, 'payload': v, **({'registered': True} if k == 'git_ssh_key' else {})}
                                        for k, v in payloads.items()}}
            return {'status': 'recorded', 'resync': resync}
        return request, calls

    def test_status_parsing_never_reads_hosts_yml_or_token(self):
        runner = FakeRunner(logged_in=True)
        status = hc.gh_status(runner)
        self.assertEqual(status, {'logged_in': True, 'account': 'alexeygrigorev', 'scopes': ['gist', 'read:org', 'repo'], 'error': None})
        argv = runner.calls[0][0]
        self.assertNotIn('--show-token', argv)
        self.assertEqual(hc.gh_status(FakeRunner(logged_in=False))['logged_in'], False)

    def test_sync_logs_in_non_interactively_with_token_on_stdin_only(self):
        runner = FakeRunner(logged_in=False)
        request, calls = self.hub({'gh_token': {'token': TOKEN}})
        result = hc.sync(request, runner)
        self.assertEqual(result['applied'], ['gh_token'])
        login = [c for c in runner.calls if c[0][1:3] == ['auth', 'login']][0]
        self.assertIn('--with-token', login[0])
        self.assertEqual(login[1].strip(), TOKEN)
        self.assertTrue(all(TOKEN not in a for c in runner.calls for a in c[0]))
        self.assertNotIn('GH_TOKEN', login[2])
        report = [c for c in calls if c[1] == '/api/host-credentials/status'][0][2]
        self.assertTrue(report['gh']['logged_in'])
        self.assertEqual(report['gh']['account'], 'alexeygrigorev')
        self.assertNotIn(TOKEN, repr(calls[1:]))
        self.assertEqual(report['generations'], {'gh_token': 1})

    def test_ssh_key_added_never_replaces_existing_config_or_keys(self):
        self.ssh.mkdir(parents=True)
        (self.ssh / 'id_rsa').write_text('EXISTING-KEY')
        (self.ssh / 'config').write_text('Host work\n  HostName example.com\n')
        (self.ssh / 'known_hosts').write_text('example.com ssh-rsa AAAA\n')
        runner = FakeRunner()
        request, _ = self.hub({'git_ssh_key': {'private_key': PRIV, 'public_key': PUB}})
        hc.sync(request, runner)
        self.assertEqual((self.ssh / 'id_rsa').read_text(), 'EXISTING-KEY')
        config = (self.ssh / 'config').read_text()
        self.assertIn('Host work\n  HostName example.com', config)
        self.assertTrue(config.startswith(hc.BLOCK_BEGIN))
        self.assertIn('IdentityFile', config)
        known = (self.ssh / 'known_hosts').read_text()
        self.assertTrue(known.startswith('example.com ssh-rsa AAAA\n'))
        self.assertIn('github.com ' + hc.GITHUB_ED25519, known)
        self.assertEqual((self.ssh / hc.KEY_NAME).read_text(), PRIV)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE((self.ssh / hc.KEY_NAME).stat().st_mode), 0o600)

    def test_windows_acl_is_restricted_before_any_bytes_exist(self):
        """Review finding: 0o600 is ignored on Windows, so the ACL must be locked while the tmp file is still empty."""
        real_open = os.open
        events = []

        def fake_open(path, flags, mode=0o600):
            events.append(('open', bool(flags & os.O_CREAT)))
            return real_open(path, flags, mode)

        def fake_restrict(path):
            events.append(('restrict', Path(path).stat().st_size))

        self.ssh.mkdir(parents=True)
        with mock.patch.object(hc, '_is_windows', return_value=True), mock.patch.object(hc.os, 'open', side_effect=fake_open), \
                mock.patch.object(hc, '_restrict_windows', side_effect=fake_restrict):
            hc._write_private(self.ssh / hc.KEY_NAME, PRIV)
        self.assertEqual(events, [('open', True), ('restrict', 0), ('open', False)])
        self.assertEqual((self.ssh / hc.KEY_NAME).read_text(), PRIV)

    def test_ssh_config_block_is_idempotent_and_replaced_in_place(self):
        block = hc._config_block(Path('/home/u/.ssh/' + hc.KEY_NAME))
        once = hc.merge_ssh_config('Host a\n  User x\n', block)
        self.assertEqual(hc.merge_ssh_config(once, block), once)
        moved = hc.merge_ssh_config(once, block.replace('/home/u', '/home/v'))
        self.assertEqual(moved.count(hc.BLOCK_BEGIN), 1)
        self.assertIn('/home/v/', moved)
        self.assertTrue(moved.rstrip().endswith('User x'))
        known = hc.merge_known_hosts('github.com ' + hc.GITHUB_ED25519 + '\n', [hc.GITHUB_ED25519])
        self.assertEqual(known.count('ssh-ed25519'), 1)

    def test_identity_applied_only_when_different(self):
        runner = FakeRunner()
        request, _ = self.hub({'git_identity': {'name': 'Agent Hub', 'email': 'a@example.com'}})
        hc.sync(request, runner)
        self.assertEqual(runner.git, {'user.name': 'Agent Hub', 'user.email': 'a@example.com'})
        writes = len([c for c in runner.calls if c[0][-2:-1] and c[0][0].endswith('git') and '--get' not in c[0]])
        hc.apply_identity({'name': 'Agent Hub', 'email': 'a@example.com'}, runner)
        self.assertEqual(len([c for c in runner.calls if c[0][0].endswith('git') and '--get' not in c[0]]), writes)

    def test_failed_login_keeps_old_generation_so_next_poll_retries(self):
        runner = FakeRunner()
        request, calls = self.hub({'gh_token': {'token': 'rejected-by-gh'}})
        result = hc.sync(request, runner)
        self.assertEqual(result['errors'], {'gh_token': 'gh_login_failed'})
        state = hc._load_state()
        self.assertEqual(state['generations'], {})
        self.assertEqual(calls[0][1], '/api/host-credentials/sync')

    def test_unregistered_key_is_held_until_github_accepts_it(self):
        runner = FakeRunner(logged_in=False)
        payloads = {'git_ssh_key': {'private_key': PRIV, 'public_key': PUB}, 'gh_token': {'token': TOKEN}}

        def request(method, path, body=None):
            if path.startswith('/api/host-credentials/sync'):
                return {'generations': {k: 1 for k in payloads}, 'forced': False,
                        'credentials': {k: {'generation': 1, 'payload': v, **({'registered': False} if k == 'git_ssh_key' else {})}
                                        for k, v in payloads.items()}}
            return {'status': 'recorded', 'resync': True}

        result = hc.sync(request, runner)
        # The gh login is applied; the shared key is NOT installed and its
        # generation is remembered so Hub does not re-send it every poll.
        self.assertEqual(result['applied'], ['gh_token'])
        self.assertEqual(result['errors'], {'git_ssh_key': 'awaiting_github_registration'})
        self.assertFalse((self.ssh / hc.KEY_NAME).exists())
        # The held key must not masquerade as an applied ssh credential: its
        # generation is remembered so Hub does not re-send it every poll.
        self.assertEqual(hc._load_state()['generations'].get('git_ssh_key'), 1)

    def test_registered_key_installs_and_authenticates(self):
        runner = FakeRunner(logged_in=True)
        request, _ = self.hub({'git_ssh_key': {'private_key': PRIV, 'public_key': PUB}})
        result = hc.sync(request, runner)
        self.assertEqual(result['applied'], ['git_ssh_key'])
        self.assertTrue((self.ssh / hc.KEY_NAME).exists())
        self.assertEqual(result.get('errors'), None)

    def test_hub_resync_after_logged_out_report_fetches_again_once(self):
        runner = FakeRunner(logged_in=False)
        seen = []

        def request(method, path, body=None):
            seen.append(path)
            if path.startswith('/api/host-credentials/sync'):
                return {'generations': {'gh_token': 1}, 'credentials': {} if len([p for p in seen if 'sync' in p]) == 1 else {'gh_token': {'generation': 1, 'payload': {'token': TOKEN}}}}
            return {'status': 'recorded', 'resync': not runner.logged_in}
        # First poll: host already holds generation 1 (state), but gh was logged out outside Hub.
        hc._save_state({'generations': {'gh_token': 1}})
        result = hc.sync(request, runner)
        self.assertTrue(runner.logged_in)
        self.assertEqual(len([p for p in seen if 'sync' in p]), 2)
        self.assertEqual(result['status'], 'synced')

    def test_poll_cadence_skips_between_polls(self):
        runner = FakeRunner(logged_in=True)
        request, calls = self.hub({})
        clock = mock.Mock(side_effect=[1000.0, 1000.0, 1010.0] + [1010.0] * 5)
        hc.sync(request, runner, clock)
        self.assertEqual(hc.sync(request, runner, clock), {'status': 'skipped'})

    def test_tick_never_raises(self):
        def boom(*a, **k):
            raise OSError('/secret/path ghp_leak')
        self.assertEqual(hc.tick(boom), {'status': 'held', 'reason': 'OSError'})


if __name__ == '__main__':
    unittest.main()


class OperatorCliTest(unittest.TestCase):
    def setUp(self):
        hc._last.update(poll=0.0, status=0.0, body=None)

    def ns(self, **kw):
        import argparse
        return argparse.Namespace(**kw)

    def test_set_gh_token_from_gh_goes_to_hub_and_is_not_returned(self):
        sent = []
        runner = mock.Mock(return_value=done(0, TOKEN + '\n'))
        with mock.patch.object(hc, 'find_gh', return_value='/usr/bin/gh'):
            out = hc.command(self.ns(hostcred_cmd='set-gh-token', from_gh=True, token_file=None, stdin=False),
                             request=lambda m, p, b=None: sent.append((m, p, b)) or {'status': 'stored'}, runner=runner)
        self.assertEqual(sent, [('PUT', '/api/host-credentials/gh_token', {'payload': {'token': TOKEN}})])
        self.assertNotIn(TOKEN, repr(out))

    def test_generate_key_refuses_to_replace_without_rotate_and_discards_local_copy(self):
        listing = {'kinds': [{'kind': 'git_ssh_key'}]}
        with self.assertRaises(ValueError):
            hc.command(self.ns(hostcred_cmd='generate-ssh-key', rotate=False), request=lambda m, p, b=None: listing)
        seen = {}

        def request(method, path, body=None):
            if method == 'GET':
                return {'kinds': []}
            seen.update(body['payload'])
            return {'status': 'stored'}
        out = hc.command(self.ns(hostcred_cmd='generate-ssh-key', rotate=False), request=request)
        self.assertTrue(seen['private_key'].startswith('-----BEGIN OPENSSH PRIVATE KEY-----'))
        self.assertTrue(seen['public_key'].startswith('ssh-ed25519 '))
        self.assertNotIn('PRIVATE', repr(out))

    def test_error_message_exposes_only_slugs(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError('u', 502, 'x', {}, io.BytesIO(b'{"status":"missing_scope_admin_public_key"}'))
        self.assertEqual(hc.error_message(err), 'host-credentials refused by Hub: missing_scope_admin_public_key')
        self.assertEqual(hc.error_message(OSError('/home/x ghp_secret')), 'host-credentials failed: OSError')
