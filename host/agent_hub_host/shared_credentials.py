"""Hub-shared host credentials on the machine: gh CLI auth + shared git SSH key (54d5b703).

Hub publishes two host-level credential kinds (separate from every engine
account credential): a ``gh`` login used for the GitHub CLI and as git's https
credential helper, and one dedicated ``git-ssh`` keypair used additively for
git push/pull. The host materializes the current generation into Hub-owned
directories under the shared root (``<agent-hub config>/shared/gh`` and
``.../shared/git-ssh``) and reports a non-secret auth status back to Hub.

Isolation and safety rules:
- Hub-owned directories only. A machine's own ``~/.ssh``, ``~/.config/gh`` or
  ``~/.aws`` is never read, modified or deleted; the shared key is additive by
  construction because Hub-side dirs never collide with user files.
- Kinds never mix: each kind writes only into its own directory; the derived
  non-secret support files (gitconfig, ssh_config, known_hosts) are regenerated
  from the current generation.
- Nothing here prints, logs or raises with credential content; gh/ssh output is
  reduced to fixed reason codes and an account name.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import private_fs
from .shared_config import shared_root

STATE_FILE = 'credentials-state.json'
MAX_FILE = 16000
GIT_DIR = 'git'

# Published GitHub host keys (public, from GitHub's documented SSH key
# fingerprints); pinned here so the shared known_hosts never needs an
# accept-new first connection.
KNOWN_GITHUB_HOST_KEYS = (
    'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl',
    'github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=',
    'github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQjR2RDnDkCYI80R2KPuPrRlXy28BbzDXUA6cRyFUiuYb2MEmrNlNgFOtSJ9UoC6rKStA3fFTdFy4rrSgUhYiogF3txHzlAIYZgGBlnBzwaGhOscqI5G6BjQKVkG8JmRQyjVpTDJbYxEmPFLJtkSJDKU5VvL8iH6MXCctnsLy3DKFm2dN5zcD6SsOjVBTO1GPZZF5NLCBKfRIcxIPcNSJqDg5jctKxJyBddBd4HoRakhNUneM4g3cxLVCZLtARLXtHPBeunSd6tVd2m3a+T5nt9TPZz1BDzJYSDNbkOFmNlLVGgaYJ8SpDQeZDV2tHzisXTFxPjRBBXoQCFGvatH1sTHulSOoh2f9ndNxoE9lUn+Gm6ByG1y6mRvS1gSrgMSsxBARAv2lo6tL+WmNRXrWoFv6Z1B5RZDHtQt9flFXl7EqBpZtM4CRg6RMKMzuU0XCBHy0pBNF95vhySDNx/c3d3BqtAOk2krx6tUvWw=',
)

GIT_CONFIG_FRAGMENT = """\
# Written by agent-hub shared credentials; the gh https helper uses the
# Hub-owned GH_CONFIG_DIR set by engine_environment().
[credential "https://github.com"]
\thelper = !gh auth git-credential
[credential "https://gist.github.com"]
\thelper = !gh auth git-credential
"""


def _iso_now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def kind_dir(kind):
    return shared_root() / kind


def git_dir():
    return shared_root() / GIT_DIR


def gh_config_dir():
    return kind_dir('gh')


def git_ssh_dir():
    return kind_dir('git-ssh')


def ssh_config_path():
    return git_ssh_dir() / 'ssh_config'


def git_config_path():
    return git_dir() / 'gitconfig'


def _read_state():
    try:
        state = json.loads(private_fs.read_bytes(shared_root() / STATE_FILE, 65536))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return state if isinstance(state, dict) and isinstance(state.get('digests'), dict) else {}


def _write_state(state):
    try:
        private_fs.atomic_write(shared_root() / STATE_FILE, json.dumps(state).encode(), 65536, engine_owned=True)
    except OSError:
        pass


def _write_private(target: Path, data: bytes):
    private_fs.atomic_write(target, data, MAX_FILE, engine_owned=True)
    if os.name != 'nt':
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass


def _write_public(target: Path, data: bytes):
    private_fs.atomic_write(target, data, MAX_FILE, engine_owned=True)
    if os.name != 'nt':
        try:
            os.chmod(target, 0o644)
        except OSError:
            pass


def _digest(data: bytes):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _derived_support(kind_dir_path: Path, files: dict):
    """Regenerate the non-secret support files from the current generation."""
    written = []
    if 'gh/hosts.yml' in files:
        gitconfig = git_config_path()
        try:
            private_fs.atomic_write(gitconfig, GIT_CONFIG_FRAGMENT.encode(), 65536, engine_owned=True)
            written.append(gitconfig)
        except OSError:
            pass
    if 'git-ssh/id_ed25519' in files:
        ssh_config = ssh_config_path()
        key_path = kind_dir_path / 'id_ed25519'
        known = git_ssh_dir() / 'known_hosts'
        lines = [
            'Host github.com',
            '\tUser git',
            '\tIdentityFile %s' % key_path,
            '\tIdentitiesOnly yes',
            '\tUserKnownHostsFile %s' % known,
            '\tStrictHostKeyChecking yes',
            '\tBatchMode yes',
            '',
        ]
        try:
            private_fs.atomic_write(ssh_config, '\n'.join(lines).encode(), 65536, engine_owned=True)
            private_fs.atomic_write(known, ('\n'.join(KNOWN_GITHUB_HOST_KEYS) + '\n').encode(), 65536, engine_owned=True)
            if os.name != 'nt':
                for target in (ssh_config, known):
                    try:
                        os.chmod(target, 0o644)
                    except OSError:
                        pass
            written.extend([ssh_config, known])
        except OSError:
            pass
    return written


def reconcile(kinds_payload):
    """Materialize Hub's current shared generations; Hub is the only writer.

    ``kinds_payload`` is the ``kinds`` object from ``GET /api/shared-credentials``:
    ``{kind: {generation, files}}``. Unlike engine credentials there is no local
    write-back: hosts never rotate shared credentials, they adopt or report.
    Returns a non-secret status dict.
    """
    if not isinstance(kinds_payload, dict):
        return {'status': 'no_kinds'}
    state = _read_state()
    digests = dict(state.get('digests') or {})
    generations = dict(state.get('generations') or {})
    results = {}
    for kind, entry in kinds_payload.items():
        if not isinstance(entry, dict) or not isinstance(entry.get('files'), dict) or not isinstance(entry.get('generation'), int):
            continue
        dest = kind_dir(kind)
        materialized = []
        processed = 0
        for rel, text in sorted(entry['files'].items()):
            if not isinstance(rel, str) or not rel.startswith(kind + '/') or not isinstance(text, str) or not text:
                continue
            processed += 1
            target = dest / rel.split('/', 1)[1]
            data = text.encode('utf-8')
            secret = rel.endswith(('hosts.yml', 'id_ed25519'))
            try:
                current = private_fs.read_bytes(target, MAX_FILE, private=False) if target.is_file() else None
            except (FileNotFoundError, PermissionError, OSError):
                current = None
            if current != data:
                (_write_private if secret else _write_public)(target, data)
                materialized.append(rel)
            digests[rel] = _digest(data)
        if not processed:
            continue  # nothing in this entry belongs to the kind: never materialize or record it
        _derived_support(dest, entry['files'])
        generations[kind] = entry['generation']
        results[kind] = {'generation': entry['generation'], 'status': 'materialized' if materialized else 'in_step'}
    _write_state({'generations': generations, 'digests': digests})
    return {'status': 'reconciled' if results else 'no_kinds', 'kinds': results}


def sync_shared(request):
    """Heartbeat path: fetch, materialize, and report gh auth status to Hub.

    The ack carries the bounded non-secret gh auth summary; when a materialized
    token is present but gh still reports logged out, a rejection is recorded so
    the operator sees it in Hub (Hub re-syncs the same generation on the next
    heartbeat; a new generation clears rejections).
    """
    response = request('GET', '/api/shared-credentials')
    kinds = response.get('kinds') if isinstance(response, dict) else None
    result = reconcile(kinds if isinstance(kinds, dict) else {})
    gh = result.get('kinds', {}).get('gh') if isinstance(result.get('kinds'), dict) else None
    if isinstance(gh, dict):
        status = gh_auth_status()
        try:
            request('POST', '/api/shared-credentials/gh/ack', {'generation': gh['generation'], 'gh_auth': status})
        except Exception:
            pass
        if gh['status'] in ('materialized', 'in_step') and status.get('logged_in') is False:
            try:
                request('POST', '/api/shared-credentials/gh/rejection', {'generation': gh['generation'], 'reason': 'gh_auth_missing'})
            except Exception:
                pass
    ssh = result.get('kinds', {}).get('git-ssh') if isinstance(result.get('kinds'), dict) else None
    if isinstance(ssh, dict):
        try:
            request('POST', '/api/shared-credentials/git-ssh/ack', {'generation': ssh['generation']})
        except Exception:
            pass
    return result


def gh_auth_status(gh_config=None, timeout=15):
    """Bounded non-secret gh CLI auth summary; content is never included."""
    config_dir = Path(gh_config) if gh_config is not None else gh_config_dir()
    binary = shutil.which('gh')
    checked_at = _iso_now()
    if not binary or not (config_dir / 'hosts.yml').is_file():
        return {'installed': bool(binary), 'logged_in': False, 'account': None, 'scopes': None, 'checked_at': checked_at}
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP', 'HOME', 'USERPROFILE') if k in os.environ}
    env['GH_CONFIG_DIR'] = str(config_dir)
    try:
        proc = subprocess.run([binary, 'auth', 'status'], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False, text=True, errors='replace')
    except (OSError, subprocess.SubprocessError):
        return {'installed': True, 'logged_in': False, 'account': None, 'scopes': None, 'checked_at': checked_at}
    output = proc.stdout or ''
    account = re.search(r'Logged in to github\.com account (\S+)', output)
    scopes = re.search(r'^\s*(?:[-\u2013-]\s*)?Scopes?:\s*(.+)$', output, re.MULTILINE)
    logged_in = 'Logged in to github.com account' in output
    return {'installed': True, 'logged_in': logged_in,
            'account': account.group(1) if account else None,
            'scopes': [s for s in scopes.group(1).replace(',', ' ').split() if s] if scopes and logged_in else ([] if logged_in else None),
            'checked_at': checked_at}


def _gh_success_line(output: str):
    m = re.search(r"Hi (\S+)! You've successfully authenticated", output)
    return m.group(1) if m else None


def git_ssh_proof(timeout=20, ssh_binary=None):
    """AC8 helper: authenticate to github.com with the shared key only.

    Returns ``{ok, account}``; raw ssh output (paths, hints) never leaves this
    function — only the authenticated account name from GitHub's fixed banner.
    """
    config_path = ssh_config_path()
    if not config_path.is_file():
        return {'ok': False, 'account': None, 'reason': 'ssh_config_missing'}
    ssh = ssh_binary or shutil.which('ssh')
    if not ssh:
        return {'ok': False, 'account': None, 'reason': 'ssh_missing'}
    try:
        proc = subprocess.run([ssh, '-F', str(config_path), '-o', 'BatchMode=yes', '-T', 'git@github.com'],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False, text=True, errors='replace')
    except (OSError, subprocess.SubprocessError):
        return {'ok': False, 'account': None, 'reason': 'ssh_unreachable'}
    account = _gh_success_line(proc.stdout or '')
    return {'ok': account is not None, 'account': account, 'reason': None if account else 'ssh_auth_failed'}


def gh_scratch_proof(scratch=None, timeout=15):
    """AC5 helper: prove the shared token in a throwaway GH_CONFIG_DIR.

    Copies the materialized hosts.yml into a fresh scratch directory (never the
    user's real gh config), runs ``gh auth status`` there, removes the scratch.
    Same bounded return shape as ``gh_auth_status``.
    """
    source = gh_config_dir() / 'hosts.yml'
    if not source.is_file():
        return {'installed': shutil.which('gh') is not None, 'logged_in': False, 'account': None, 'scopes': None,
                'checked_at': _iso_now(), 'scratch': True, 'reason': 'not_materialized'}
    binary = shutil.which('gh')
    if not binary:
        return {'installed': False, 'logged_in': False, 'account': None, 'scopes': None,
                'checked_at': _iso_now(), 'scratch': True, 'reason': 'gh_missing'}
    import tempfile
    created = scratch is None
    scratch = Path(scratch) if scratch is not None else Path(tempfile.mkdtemp(prefix='agent-hub-gh-proof-'))
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        private_fs.atomic_write(scratch / 'hosts.yml', source.read_bytes(), MAX_FILE, engine_owned=True)
        return dict(gh_auth_status(gh_config=scratch, timeout=timeout), scratch=True, reason=None)
    finally:
        if created:
            import shutil as _shutil
            try:
                _shutil.rmtree(scratch, ignore_errors=True)
            except OSError:
                pass


def git_push_dry_run(repo, remote='origin', branch='HEAD', timeout=60):
    """AC8 helper: dry-run push over the shared SSH key; no refs are updated."""
    config_path = ssh_config_path()
    if not config_path.is_file():
        return {'ok': False, 'reason': 'ssh_config_missing'}
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP', 'HOME', 'USERPROFILE') if k in os.environ}
    env['GIT_SSH_COMMAND'] = 'ssh -F %s' % config_path
    env['GIT_TERMINAL_PROMPT'] = '0'
    try:
        proc = subprocess.run(['git', '-C', str(repo), 'push', '--dry-run', remote, branch], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False, text=True, errors='replace')
    except (OSError, subprocess.SubprocessError):
        return {'ok': False, 'reason': 'git_unreachable'}
    return {'ok': proc.returncode == 0, 'reason': None if proc.returncode == 0 else 'push_dry_run_failed'}


def add_parser(sub) -> None:
    sc = sub.add_parser('shared-credentials', help='Hub-shared host credentials (gh auth, shared git SSH key): sync, status, proofs')
    scsub = sc.add_subparsers(dest='shared_credentials_cmd', required=True)
    scsub.add_parser('sync', help='Fetch and materialize Hub-shared credentials now, then report gh auth status to Hub')
    scsub.add_parser('status', help='Local materialization state plus non-secret gh auth summary (no tokens)')
    proof = scsub.add_parser('proof', help='Scratch proofs: gh auth via a throwaway GH_CONFIG_DIR, ssh -T git@github.com, dry-run push')
    proof.add_argument('--repo', help='Repository for the dry-run push (default: current directory)')
    proof.add_argument('--branch', default='HEAD', help='Branch to dry-run push (default: HEAD)')


def run(args) -> None:
    """CLI entry; prints non-secret JSON only and never echoes credential content."""
    import sys
    try:
        if args.shared_credentials_cmd == 'sync':
            from . import provisioning
            print(json.dumps(sync_shared(provisioning.host_request), sort_keys=True))
        elif args.shared_credentials_cmd == 'status':
            state = _read_state()
            print(json.dumps({'generations': state.get('generations') or {}, 'gh_auth': gh_auth_status(),
                              'paths': {'gh_config': str(gh_config_dir()), 'git_ssh': str(git_ssh_dir()), 'git_config': str(git_config_path())}}, sort_keys=True))
        else:
            proof = {'gh_scratch': gh_scratch_proof(), 'git_ssh': git_ssh_proof()}
            repo = getattr(args, 'repo', None)
            if repo:
                proof['push_dry_run'] = git_push_dry_run(repo, branch=getattr(args, 'branch', 'HEAD'))
            print(json.dumps(proof, sort_keys=True))
    except Exception as exc:
        # Fixed diagnosis only; never echo server bodies or credential content.
        print('hub shared-credentials: %s' % (str(exc)[:120] or exc.__class__.__name__), file=sys.stderr)
        sys.exit(1)
