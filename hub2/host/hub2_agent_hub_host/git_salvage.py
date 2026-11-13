"""Best-effort, non-forcing preservation of committed work after verified exit."""
import os
import re
import subprocess
import time
from pathlib import Path


def salvage_branch(workspace, timeout=30):
    """Push only the pinned current branch tip. Never commit or change checkout.

    Discover the destination's default branch live, failing closed when it is
    unknown. Do not put remote URLs, Git stderr or credentials in receipts.
    The total command budget also bounds authentication/network failures.
    """
    deadline = time.monotonic() + timeout
    evidence = None
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}

    def git(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired('git', timeout)
        return subprocess.run(['git', '-C', str(workspace), *args], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=remaining)

    try:
        root = git('rev-parse', '--show-toplevel')
        if root.returncode or not Path(root.stdout.strip()).samefile(workspace):
            # A non-repository workspace nested beneath some other checkout
            # must never cause that parent checkout's branch to be published.
            return None
        remotes = git('remote')
        names = remotes.stdout.splitlines()
        if remotes.returncode or not names:
            return None
        # Prefer the repository's ordinary origin. Ambiguous destinations are
        # not guessed, and branch tracking an upstream default is not followed.
        remote = 'origin' if 'origin' in names else names[0] if len(names) == 1 else None
        if remote is None or remote.startswith('-'):
            return None
        branch = git('symbolic-ref', '--quiet', 'HEAD')
        head = git('rev-parse', '--verify', 'HEAD^{commit}')
        if branch.returncode or head.returncode:
            return None
        ref, sha = branch.stdout.strip(), head.stdout.strip()
        if not ref.startswith('refs/heads/'):
            return None
        branch = ref[len('refs/heads/'):]
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
            return None
        evidence = {'repo': root.stdout.strip(), 'remote': remote, 'branch': branch,
                    'head_sha': sha, 'status': 'failed'}
        # Query the push destination, which may differ from the fetch URL.
        urls = git('remote', 'get-url', '--push', '--all', remote)
        destinations = urls.stdout.splitlines()
        if urls.returncode or len(destinations) != 1:
            return {**evidence, 'reason': 'ambiguous_push_destination'}
        destination = destinations[0]
        refs = git('ls-remote', '--symref', '--', destination, 'HEAD', 'refs/heads/' + branch)
        if refs.returncode:
            return {**evidence, 'reason': 'remote_unavailable'}
        default = next((line.split('\t')[0][len('ref: refs/heads/'):]
                        for line in refs.stdout.splitlines()
                        if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD')), None)
        if not default:
            return {**evidence, 'reason': 'default_branch_unknown'}
        if branch == default:
            return None
        remote_sha = next((line.split('\t')[0] for line in refs.stdout.splitlines()
                           if line.endswith('\trefs/heads/' + branch)), None)
        if remote_sha == sha:
            return {**evidence, 'status': 'already_present'}
        if remote_sha and git('merge-base', '--is-ancestor', sha, remote_sha).returncode == 0:
            return {**evidence, 'status': 'already_present'}
        # Use the single verified push URL, not a possibly multi-destination
        # remote, and an explicit SHA/refspec unaffected by push.default/mirror.
        pushed = git('push', '--porcelain', '--no-force', '--no-follow-tags',
                     '--recurse-submodules=no', '--', destination, sha + ':refs/heads/' + branch)
        return {**evidence, 'status': 'pushed' if pushed.returncode == 0 else 'failed',
                **({'reason': 'push_rejected'} if pushed.returncode else {})}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**evidence, 'reason': 'git_unavailable_or_timeout'} if evidence else None
