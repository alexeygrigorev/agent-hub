"""Installed Git pre-push gate for Hub worker repositories (stdlib only).

Executed by filename too: the managed hook has a durable copy outside worktrees.
No environment variable or cached pass can skip verification of a main update.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

MARKER = '# Agent Hub managed pre-push gate v1\n'
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
# Public lock contract from worktree_guard.lock_reason/parse_lock_reason.
HUB_LOCK = re.compile(r'hub-session [0-9a-fA-F-]{8,64} intake [A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


class PrepushHookInstallError(ValueError):
    """Fixed diagnostic type, using the existing server worktree-refusal reason."""
    def __init__(self):
        super().__init__('worktree_lock_failed: checkout could not be protected')


def git(root, *args, git_bin='git'):
    return subprocess.check_output([git_bin, *args], cwd=root, text=True,
                                   stderr=subprocess.PIPE, timeout=60).strip()


def atomic_write(path, body, mode):
    fd, name = tempfile.mkstemp(prefix='.install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(body)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def optional_config(root, name, git_bin):
    try:
        return git(root, 'config', '--local', '--get', name, git_bin=git_bin)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return None
        raise


def required(root, git_bin='git'):
    """Keep the project policy even when a candidate removes its test lane."""
    if optional_config(root, 'hub.prepushRequired', git_bin) == 'true':
        return True
    origin = optional_config(root, 'remote.origin.url', git_bin) or ''
    return bool(re.search(r'[:/]alexeygrigorev/agent-hub(?:\.git)?/?$', origin)
                or ((Path(root) / 'scripts/test-affected-local.sh').is_file()
                    and (Path(root) / 'host/agent_hub_host').is_dir()))


def install(root, git_bin='git'):
    """Install a durable per-worktree hook, preserving inherited hooks.

    Only this worktree's core.hooksPath changes. Enabling worktreeConfig is a
    repository capability; the main checkout keeps its existing hooks setting.
    """
    gitdir = Path(git(root, 'rev-parse', '--absolute-git-dir', git_bin=git_bin))
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir', git_bin=git_bin))
    if gitdir == common:
        raise RuntimeError('install from a linked worker worktree; main checkout is unchanged')
    state = gitdir / 'hub-prepush'
    hooks = state / 'hooks'
    active = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path',
                      'hooks', git_bin=git_bin))
    if optional_config(root, 'core.worktree', git_bin) or optional_config(root, 'core.bare', git_bin) == 'true':
        raise RuntimeError('worktreeConfig needs a repository configuration migration first')
    if active == hooks:
        previous = Path((state / 'previous-hooks').read_text().strip())
    else:
        previous = active
    hooks.mkdir(parents=True, exist_ok=True)
    atomic_write(state / 'previous-hooks', str(previous), 0o644)
    gate = state / 'gate.py'
    atomic_write(gate, Path(__file__).read_text(), 0o644)
    # Keep inherited hooks effective instead of disabling commit/other guards
    # when assigning this worktree its own hooks directory.
    if previous.is_dir():
        for prior in previous.iterdir():
            if prior.name == 'pre-push' or prior.name.endswith('.sample') or not prior.is_file():
                continue
            if not os.access(prior, os.X_OK):
                continue
            target = hooks / prior.name
            if target.exists() and MARKER not in target.read_text()[:256]:
                raise RuntimeError('unmanaged file in Hub-owned hooks directory')
            atomic_write(target, '#!/bin/sh\n' + MARKER + 'exec '
                         + shlex.quote(str(prior)) + ' "$@"\n', 0o755)
    hook = hooks / 'pre-push'
    if hook.is_symlink() or (hook.exists() and MARKER not in hook.read_text()[:256]):
        raise RuntimeError('unmanaged pre-push hook in Hub-owned hooks directory')
    atomic_write(hook, '#!/bin/sh\n' + MARKER + 'exec ' + shlex.quote(sys.executable)
                 + ' ' + shlex.quote(str(gate)) + ' hook "$@"\n', 0o755)
    git(root, 'config', '--local', 'extensions.worktreeConfig', 'true', git_bin=git_bin)
    git(root, 'config', '--worktree', 'core.hooksPath', str(hooks), git_bin=git_bin)
    git(root, 'config', '--local', 'hub.prepushRequired', 'true', git_bin=git_bin)


@contextmanager
def release_lock(root):
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    with (common / 'hub-push-main.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def clean(root):
    return not git(root, 'status', '--porcelain', '--untracked-files=all')


def install_workers(root):
    """Roll out only to Hub-bound workers; preserve human/integration trees."""
    installed = 0
    records, current = [], {}
    for field in git(root, 'worktree', 'list', '--porcelain', '-z').split('\0'):
        if field.startswith('worktree '):
            if current:
                records.append(current)
            current = {'path': field[len('worktree '):]}
        elif field.startswith('locked '):
            current['lock'] = field[len('locked '):].strip()
    if current:
        records.append(current)
    for record in records:
        worker = Path(record['path'])
        if (HUB_LOCK.fullmatch(record.get('lock', ''))
                and (worker / '.git').is_file() and required(worker)):
            install(worker)
            installed += 1
    print(f'push-main: installed per-worktree gate for {installed} worker worktrees')


def verify(root, candidate, base):
    if not SHA.fullmatch(candidate) or not SHA.fullmatch(base) or set(base) == {'0'}:
        raise RuntimeError('main deletion/creation or invalid ref; existing main base required')
    if set(candidate) == {'0'}:
        raise RuntimeError('main deletion refused')
    if git(root, 'rev-parse', 'HEAD') != candidate or not clean(root):
        raise RuntimeError('push must name the committed HEAD of a clean working tree')
    git(root, 'merge-base', '--is-ancestor', base, candidate)
    # Git exports GIT_DIR/GIT_INDEX_FILE/... to hooks; tests that create
    # fixture repos would otherwise commit into (and rewrite) this worktree.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['AGENT_HUB_HEAVY_TEST_PRIORITY'] = 'final'
    retry = root / 'scripts/capped-test-retry.sh'
    affected = root / 'scripts/test-affected-local.sh'
    if not retry.is_file() or not affected.is_file():
        raise RuntimeError('repository has no affected-test lane; nothing pushed')
    result = subprocess.call([str(retry), '--', str(affected), '--base', base], cwd=root, env=env)
    if result == 4:
        result = subprocess.call([str(retry), '--', str(root / 'scripts/test-full-local.sh'),
                                  '--priority', 'final'], cwd=root, env=env)
        if result:
            raise RuntimeError('full tests failed; nothing pushed')
    elif result:
        raise RuntimeError(f'affected tests failed or capacity still held (exit {result}); nothing pushed')
    if git(root, 'rev-parse', 'HEAD') != candidate or not clean(root):
        raise RuntimeError('source changed during verification; nothing pushed')
    print(f'push-main: verified {candidate} against advertised main {base}', flush=True)


def hook(root, lines, remote_url='origin'):
    updates = []
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError('invalid pre-push ref input')
        if fields[2] == 'refs/heads/main':
            updates.append(fields)
    if not updates:
        return
    if len(updates) != 1:
        raise RuntimeError('multiple main updates refused')
    _, candidate, _, base = updates[0]
    # Verification may wait for admission or run arbitrary tests. Never hold
    # the repository release lock during it (including BROAD fallback).
    verify(root, candidate, base)
    # Keep only the final remote/source comparison serialized. The hook exits
    # before its parent Git updates main; Git's remote ref transaction provides
    # the atomic old-SHA compare and lock for that update, including raw pushes.
    with release_lock(root):
        git(root, 'fetch', '--no-tags', remote_url, 'refs/heads/main')
        if git(root, 'rev-parse', 'FETCH_HEAD') != base:
            raise RuntimeError('remote main changed during verification; integrate and retry')
        if git(root, 'rev-parse', 'HEAD') != candidate or not clean(root):
            raise RuntimeError('source changed before push; nothing pushed')


def main():
    try:
        root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
        mode = sys.argv[1] if len(sys.argv) > 1 else ''
        if mode == 'install' and len(sys.argv) == 2:
            install(root)
        elif mode == 'install-workers' and len(sys.argv) == 2:
            install_workers(root)
        elif mode == 'hook' and len(sys.argv) == 4:
            lines = list(sys.stdin)
            prior_dir = Path(__file__).with_name('previous-hooks')
            if prior_dir.is_file():
                prior = Path(prior_dir.read_text().strip()) / 'pre-push'
                if prior.is_file() and os.access(prior, os.X_OK):
                    result = subprocess.run([str(prior), *sys.argv[2:]], cwd=root,
                                            input=''.join(lines), text=True)
                    if result.returncode:
                        raise RuntimeError('inherited pre-push hook refused push')
            hook(root, lines, sys.argv[3])
        elif mode == 'push' and len(sys.argv) == 2:
            if not clean(root):
                raise RuntimeError('commit working-tree changes before releasing')
            candidate = git(root, 'rev-parse', 'HEAD')
            git(root, 'fetch', '--no-tags', 'origin', 'refs/heads/main')
            install(root)
            # The installed hook is the only verifier for helper and raw pushes.
            return subprocess.call(['git', '-c', 'push.followTags=false', 'push',
                                    'origin', candidate + ':refs/heads/main'], cwd=root)
        else:
            raise RuntimeError('usage: prepush.py install|install-workers|push (hook is called by Git)')
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print('push-main: REFUSED: ' + detail, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
