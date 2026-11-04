"""Hub-shared host credentials on hosts: gh CLI auth + shared git SSH key (54d5b703).

Materialization is additive and confined to Hub-owned directories under the
shared root; a machine's own ~/.ssh, ~/.config/gh and ~/.aws are never read,
modified or deleted. Status helpers return bounded non-secret summaries only.
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from agent_hub_host import shared_credentials, shared_config


pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only proof harness; Windows path covered by CLI smoke')

GH_TOKEN = 'gho_SYNTHETICTESTTOKEN000000000000'
GH_HOSTS = 'github.com:\n    git_protocol: https\n    oauth_token: ' + GH_TOKEN + '\n    user: agent-hub-bot\n'
PRIV_KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkSYNTHETICNOTAREALKEY\n-----END OPENSSH PRIVATE KEY-----\n'
PUB_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISYNTHETICNOTAREALKEY0123456789 agent-hub-shared\n'


def kinds_payload():
    return {
        'gh': {'generation': 3, 'files': {'gh/hosts.yml': GH_HOSTS}},
        'git-ssh': {'generation': 2, 'files': {'git-ssh/id_ed25519': PRIV_KEY, 'git-ssh/id_ed25519.pub': PUB_KEY}},
    }


def stub_binary(tmp_path, name, body, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text('#!/bin/sh\n%s\n' % body)
    stub.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))
    return stub


def test_reconcile_materializes_kinds_into_hub_owned_dirs(tmp_path, monkeypatch):
    home = tmp_path / 'real-home'
    ssh_dir = home / '.ssh'
    ssh_dir.mkdir(parents=True)
    sentinel = ssh_dir / 'id_ed25519'
    sentinel.write_bytes(b'USER-OWNED-KEY-NOT-TOUCHED\n')

    result = shared_credentials.reconcile(kinds_payload())
    assert result['status'] == 'reconciled'
    assert result['kinds']['gh'] == {'generation': 3, 'status': 'materialized'}
    root = shared_config.shared_root()
    # Materialization stays in the Hub-owned shared root beside the host
    # config; it is never inside a user home's ~/.ssh.
    from agent_hub_host import config
    assert root == Path(config.get_config_path()).parent / 'shared'
    assert not str(root).startswith(str(ssh_dir))
    hosts = root / 'gh' / 'hosts.yml'
    assert hosts.read_text(encoding='utf-8') == GH_HOSTS
    assert stat.S_IMODE(hosts.stat().st_mode) & 0o077 == 0  # token file is owner-only
    key = root / 'git-ssh' / 'id_ed25519'
    assert key.read_text(encoding='utf-8') == PRIV_KEY
    assert stat.S_IMODE(key.stat().st_mode) & 0o077 == 0
    # Derived non-secret support files are regenerated for the current generation.
    ssh_config = (root / 'git-ssh' / 'ssh_config').read_text(encoding='utf-8')
    assert 'IdentityFile %s' % key in ssh_config and 'UserKnownHostsFile' in ssh_config and 'StrictHostKeyChecking yes' in ssh_config
    known = (root / 'git-ssh' / 'known_hosts').read_text(encoding='utf-8')
    assert known.startswith('github.com ssh-ed25519 ')
    gitconfig = (root / 'git' / 'gitconfig').read_text(encoding='utf-8')
    assert 'credential "https://github.com"' in gitconfig and 'gh auth git-credential' in gitconfig
    state = json.loads((root / shared_credentials.STATE_FILE).read_text(encoding='utf-8'))
    assert state['generations'] == {'gh': 3, 'git-ssh': 2}
    # The user's own key is byte-identical and ~/.ssh gained nothing.
    assert sentinel.read_bytes() == b'USER-OWNED-KEY-NOT-TOUCHED\n'
    assert list(ssh_dir.iterdir()) == [sentinel]

    again = shared_credentials.reconcile(kinds_payload())
    assert again['kinds']['gh']['status'] == 'in_step'
    assert again['kinds']['git-ssh']['status'] == 'in_step'
    assert list(ssh_dir.iterdir()) == [sentinel]


def test_reconcile_rejects_cross_kind_paths():
    payload = {'gh': {'generation': 1, 'files': {'git-ssh/id_ed25519': PRIV_KEY}}}
    result = shared_credentials.reconcile(payload)
    assert result['status'] == 'no_kinds' and result['kinds'] == {}
    # The earlier (legitimate) git-ssh generation from the materialization test
    # is untouched, and the fake gh entry was never recorded: a gh entry
    # carrying a git-ssh path neither materializes nor advances a generation.
    state = json.loads((shared_config.shared_root() / shared_credentials.STATE_FILE).read_text(encoding='utf-8'))
    assert state['generations'].get('git-ssh', 'absent') in ('absent', 2)
    assert state['generations'].get('gh') is None


def test_gh_auth_status_parses_the_non_secret_summary(tmp_path, monkeypatch):
    shared_credentials.reconcile({'gh': {'generation': 1, 'files': {'gh/hosts.yml': GH_HOSTS}}})
    stub_binary(tmp_path, 'gh', "echo 'github.com'\n"
                "echo '  Logged in to github.com account agent-hub-bot (keyring)'\n"
                "echo '  - Active account: true'\n"
                "echo '  - Git operations protocol: https'\n"
                "echo '  - Scopes: gist, repo, workflow'\n"
                "echo '  - Token: gho_****'", monkeypatch)
    status = shared_credentials.gh_auth_status()
    assert status['installed'] is True and status['logged_in'] is True
    assert status['account'] == 'agent-hub-bot'
    assert status['scopes'] == ['gist', 'repo', 'workflow']
    assert GH_TOKEN not in json.dumps(status)  # token never in the summary
    empty_bin = tmp_path / 'empty-bin'
    empty_bin.mkdir()
    monkeypatch.setenv('PATH', str(empty_bin))
    missing = shared_credentials.gh_auth_status()
    assert missing['installed'] is False and missing['logged_in'] is False


def test_sync_shared_acks_and_reports_missing_auth(tmp_path, monkeypatch):
    stub_binary(tmp_path, 'gh', "echo 'not logged in'; exit 1", monkeypatch)
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if method == 'GET':
            return {'kinds': {'gh': {'generation': 1, 'files': {'gh/hosts.yml': GH_HOSTS}},
                              'git-ssh': {'generation': 1, 'files': {'git-ssh/id_ed25519': PRIV_KEY, 'git-ssh/id_ed25519.pub': PUB_KEY}}}}
        return {'status': 'acked'}

    result = shared_credentials.sync_shared(request)
    assert result['kinds']['gh']['status'] == 'materialized'  # first reconcile in this home
    posts = [(p, a) for m, p, a in calls if m == 'POST']
    gh_ack = next(a for p, a in posts if p == '/api/shared-credentials/gh/ack')
    assert gh_ack['generation'] == 1
    assert set(gh_ack['gh_auth']) <= {'installed', 'logged_in', 'account', 'scopes', 'checked_at'}  # bounded keys only
    assert GH_TOKEN not in json.dumps(gh_ack)
    assert ('/api/shared-credentials/gh/rejection', {'generation': 1, 'reason': 'gh_auth_missing'}) in posts  # AC4 report
    assert ('/api/shared-credentials/git-ssh/ack', {'generation': 1}) in posts


def test_scratch_proof_runs_in_a_throwaway_config_dir(tmp_path, monkeypatch):
    shared_credentials.reconcile({'gh': {'generation': 1, 'files': {'gh/hosts.yml': GH_HOSTS}}})
    scratch = tmp_path / 'scratch-gh'
    scratch.mkdir(parents=True)
    scratch.chmod(0o700)  # the token copy must land in a private directory
    stub_binary(tmp_path, 'gh', "test -f \"$GH_CONFIG_DIR/hosts.yml\" && echo '  Logged in to github.com account agent-hub-bot (keyring)'", monkeypatch)
    proof = shared_credentials.gh_scratch_proof(scratch=scratch)
    assert proof['logged_in'] is True and proof['scratch'] is True
    assert proof['account'] == 'agent-hub-bot'
    # The real shared config is untouched; the explicit scratch copy remains for the caller.
    assert (shared_config.shared_root() / 'gh' / 'hosts.yml').is_file()
    assert (scratch / 'hosts.yml').is_file()

    # With no scratch given, the throwaway dir is fully removed afterwards.
    monkeypatch.setenv('TMPDIR', str(tmp_path / 'tmp'))
    (tmp_path / 'tmp').mkdir()
    auto = shared_credentials.gh_scratch_proof()
    assert auto['logged_in'] is True
    assert not list((tmp_path / 'tmp').glob('agent-hub-gh-proof-*'))


def test_git_ssh_proof_and_dry_run_use_only_the_shared_config(tmp_path, monkeypatch):
    shared_credentials.reconcile({'git-ssh': {'generation': 1, 'files': {'git-ssh/id_ed25519': PRIV_KEY, 'git-ssh/id_ed25519.pub': PUB_KEY}}})
    stub_binary(tmp_path, 'ssh', "echo \"Hi agent-hub-bot! You've successfully authenticated, but GitHub does not provide shell access.\"", monkeypatch)
    proof = shared_credentials.git_ssh_proof()
    assert proof == {'ok': True, 'account': 'agent-hub-bot', 'reason': None}

    seen = {}

    def fake_git(argv, **kwargs):
        seen['ssh_command'] = kwargs['env']['GIT_SSH_COMMAND']
        return type('P', (), {'returncode': 0, 'stdout': ''})()

    monkeypatch.setattr(shared_credentials.subprocess, 'run', fake_git)
    assert shared_credentials.git_push_dry_run('/synthetic/repo') == {'ok': True, 'reason': None}
    assert seen['ssh_command'] == 'ssh -F %s' % shared_credentials.ssh_config_path()

    # Missing materialization is a fixed reason, never an exception.
    monkeypatch.setattr(shared_credentials, 'ssh_config_path', lambda: tmp_path / 'absent-ssh-config')
    assert shared_credentials.git_ssh_proof() == {'ok': False, 'account': None, 'reason': 'ssh_config_missing'}
    assert shared_credentials.git_push_dry_run('/synthetic/repo') == {'ok': False, 'reason': 'ssh_config_missing'}
