"""Windows clone-on-demand using user-owned roots and a bounded byte lock."""
import contextlib
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time


def supported():
    if os.name != 'nt':
        return False
    try:
        import msvcrt
        import ctypes
    except ImportError:
        return False
    return (callable(getattr(msvcrt, 'locking', None)) and
            callable(getattr(msvcrt, 'open_osfhandle', None)) and
            callable(getattr(ctypes, 'WinDLL', None)))


def _plain(path):
    info = path.lstat()
    return not (stat.S_ISLNK(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & 0x400)


def _open_lock(path):
    """Pin a non-reparse Windows file without delete sharing or truncation."""
    if os.name != 'nt':
        # Deterministic byte-lock contract tests can run on a POSIX host.
        return open(path, 'a+b')
    import msvcrt
    from .win32_fs import api
    native = api()
    handle = native.open(path, 0xC0000000, disposition=4)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        native.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, 'r+b')
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def checkout_lock(path, *, timeout=30):
    import msvcrt
    # Do not truncate: sibling processes lock the same persistent inode.
    if path.exists() and not _plain(path):
        raise ValueError('repo_checkout_missing')
    with _open_lock(path) as handle:
        if not _plain(path) or not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('repo_checkout_missing')
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('repo_checkout_missing') from None
                time.sleep(0.1)
        try:
            # Windows byte locks may extend beyond EOF. Initialize only after
            # acquiring it so two first-time callers never race a locked write.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _run(argv, *, timeout, stderr=None):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=stderr if stderr is not None else subprocess.DEVNULL)
    try:
        return process.wait(timeout=timeout)
    except BaseException:
        # taskkill targets only our own process tree, including Git helpers.
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)
        process.wait(timeout=15)
        raise


# Bounded clone-output evidence (intake fae60b8a): gh/git stderr must reach
# the journal's repo_clone_failed diagnostic instead of dying in DEVNULL.
CLONE_OUTPUT_LIMIT = 16384
CLONE_DETAIL_LIMIT = 200


def _scrub_clone_text(text):
    """Redact secrets and paths from bounded clone output (never fail)."""
    try:
        from .log_redaction import redact_log
        text = redact_log(text)
    except Exception:
        pass
    text = re.sub(r'\S*[/\\]\S*', '[path]', text)
    return ''.join(ch if 32 <= ord(ch) < 127 else ' ' for ch in text)


def _short_clone_detail(raw, code):
    """First scrubbed non-empty clone-output line, or a named exit fallback."""
    if raw:
        text = raw.decode('utf-8', 'replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
        for line in text.splitlines():
            clean = _scrub_clone_text(line).strip()
            if clean:
                return clean[:CLONE_DETAIL_LIMIT]
    if type(code) is int:
        return 'exit %d' % code
    return 'clone command failed'


def _remove_staging(path):
    """Remove our clone, including Git for Windows read-only object files."""
    def retry_readonly(function, filename, exc_info):
        if not isinstance(exc_info[1], PermissionError):
            raise exc_info[1]
        os.chmod(filename, stat.S_IWRITE | stat.S_IREAD)
        function(filename)

    # A persistent cleanup failure must not replace the typed clone failure.
    with contextlib.suppress(OSError):
        if path.exists() and _plain(path):
            shutil.rmtree(path, onerror=retry_readonly)


def clone(settings, repo, origin, target_dir, *, disk_floor_gb, auth_ok, verify_origin,
          disk_state='ok', on_created=None):
    """Publish a complete clone; preserve every pre-existing destination."""
    if (not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', repo)
            or repo.endswith('.') or re.match(r'(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', repo)):
        raise ValueError('repo_checkout_missing')
    if not isinstance(origin, str) or not re.fullmatch(r'(?:https://github\.com/)?[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', origin):
        raise ValueError('repo_origin_missing')
    if isinstance(disk_floor_gb, bool) or not isinstance(disk_floor_gb, (int, float)) or not math.isfinite(disk_floor_gb) or disk_floor_gb < 1:
        raise ValueError('invalid_disk_floor')
    config = settings.get('repos') or {}
    if not isinstance(config, dict):
        raise ValueError('repo_checkout_missing')
    try:
        target = Path(target_dir)
        roots = [Path(root).resolve(strict=True) for root in settings.get('workspace_roots', [])]
        root = target.parent.resolve(strict=True)
        configured = Path(config.get('checkout_root') or str(roots[0] if roots else '')).resolve(strict=True)
        if (not target.is_absolute() or target.name != repo or root != target.parent or root != configured
                or not any(root == allowed or allowed in root.parents for allowed in roots)
                or any(not _plain(parent) for parent in (root, *root.parents))
            or config.get('clone_on_demand', True) is not True):
            raise ValueError('repo_checkout_missing')
    except (OSError, RuntimeError, TypeError) as exc:
        raise ValueError('repo_checkout_missing') from exc
    with checkout_lock(root / ('.hub-clone-' + repo + '.lock')):
        if target.exists():
            if not _plain(target) or not target.is_dir() or not (target / '.git').is_dir() or not _plain(target / '.git'):
                raise ValueError('repo_checkout_missing')
            verify_origin(target, origin)
            return target
        try:
            usage = shutil.disk_usage(root)
        except OSError as exc:
            raise ValueError('host_disk_warn_no_clone') from exc
        if (not all(isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
                    for value in (usage.total, usage.used, usage.free))
                or usage.total <= 0 or usage.used > usage.total or usage.free > usage.total
                or disk_state != 'ok' or usage.used / usage.total >= .85
                or usage.free / 1024**3 < disk_floor_gb):
            raise ValueError('host_disk_warn_no_clone')
        if not auth_ok():
            raise ValueError('host_gh_auth_unavailable')
        # The unique directory is owned by this invocation before any effects.
        staging = Path(tempfile.mkdtemp(prefix='.hub-clone-' + repo + '-', dir=root))
        try:
            argv = ['gh', 'repo', 'clone', origin, str(staging), '--', '--origin', 'origin', '--no-tags']
            clone_filter = config.get('clone_filter', 'blob:none')
            if clone_filter:
                argv.append('--filter=' + clone_filter)
            # Intake fae60b8a: capture bounded gh/git stderr so the failure
            # record carries the real clone error, never a bare exit code.
            # A pipe would risk deadlock on voluminous gh progress; the
            # disk-backed temp file cannot block the child.
            with tempfile.TemporaryFile(prefix='hub-clone-err-') as err:
                code = _run(argv, timeout=config.get('clone_timeout_sec', 300), stderr=err)
                try:
                    err.seek(0)
                    captured = err.read(CLONE_OUTPUT_LIMIT + 1)
                except OSError:
                    captured = b''
            if code != 0:
                raise ValueError('repo_clone_failed: ' + _short_clone_detail(captured, code))
            if not _plain(staging) or not (staging / '.git').is_dir() or not _plain(staging / '.git'):
                raise ValueError('repo_clone_failed: incomplete checkout')
            verify_origin(staging, origin)
            if target.exists() or target.is_symlink():
                raise ValueError('repo_checkout_missing')
            # Windows rename refuses an existing destination, including a race.
            staging.rename(target)
            if on_created:
                on_created(target)
            return target
        except (OSError, subprocess.SubprocessError) as exc:
            # Never a bare slug: name the scrubbed OS/process cause.
            detail = _short_clone_detail(str(exc), None)
            raise ValueError('repo_clone_failed: ' + detail) from exc
        finally:
            _remove_staging(staging)
