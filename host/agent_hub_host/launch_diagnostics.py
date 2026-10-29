"""Bounded launch diagnostics: never persist arbitrary process output or argv."""
from __future__ import annotations
import datetime as dt
import os
from pathlib import Path
import re
import time
import json

# Match complete known launcher refusal lines, including traceback final lines.
# Messages are constants: suffixes, payloads, paths and credentials never survive.
REFUSALS = {
    'Launcher temporary directory unavailable': ('launcher_tmpdir_unavailable', 'The launcher temporary directory could not be safely prepared.'),
    'worktree_lock_failed: missing offer identity': ('launcher_worktree_lock_failed', 'The launch has no worktree protection identity.'),
    'worktree_lock_failed: missing canonical session identity': ('launcher_worktree_lock_failed', 'The launch has no canonical session identity for worktree protection.'),
    'worktree_lock_failed: checkout could not be protected': ('launcher_worktree_lock_failed', 'The checkout could not be protected; an existing session or operator lock is preserved.'),
    'worktree_identity_failed: original repository could not be recorded': ('launcher_worktree_identity_failed', 'The original worktree repository identity could not be recorded before native start.'),
    'launcher_disk_reserve': ('launcher_disk_reserve', 'Host disk headroom after launcher reservations is below the maintained disk floor.'),
    'Worker Hub credential could not be prepared': ('launcher_worker_auth_unavailable', 'Worker Hub credential could not be prepared.'),
    'memory_pressure': ('memory_pressure', 'Host memory full pressure exceeds the safety cap.'),
    'host_oom': ('host_oom', 'A recent host OOM kill requires a placement cooldown.'),
    # Also registers the fixed summary accepted by launcher_recovery's
    # no-start settlement; the numeric policy value stays in the bounded tail.
    'launcher_memory_reserve': ('launcher_memory_reserve', 'Host memory headroom after launcher reservations is below the configured floor.'),
    'host_runtime_choking': ('host_runtime_choking', 'Recent session or service failures require a placement cooldown.'),
    'cpu_headroom': ('cpu_headroom', 'Host CPU usage plus expected session demand exceeds the CPU target.'),
    'cpu_pressure': ('cpu_pressure', 'Host CPU pressure exceeds the safety cap.'),
    'cpu_usage_unknown': ('cpu_usage_unknown', 'Fresh real CPU usage is unavailable.'),
    'host MemAvailable < 10GiB': ('launcher_memory_reserve', 'Host memory headroom after launcher reservations is below 10 GiB.'),
    'Host memory headroom after launcher reservations is below 10 GiB.': ('launcher_memory_reserve', 'Host memory headroom after launcher reservations is below 10 GiB.'),
    'Host MemAvailable after reservations must retain 10 GiB': ('launcher_memory_reserve', 'Host memory headroom after launcher reservations is below 10 GiB.'),
    'requested worker memory > 1500MiB': ('launcher_worker_memory', 'Requested worker memory exceeds 1500 MiB.'),
    'Requested worker memory > 1500MiB': ('launcher_worker_memory', 'Requested worker memory exceeds 1500 MiB.'),
    'No valid routes available': ('launcher_admission_refused', 'Fresh launcher admission found no valid provider route.'),
    'Configured Codex launch wrapper unavailable': ('launcher_codex_wrapper_unavailable', 'The configured protected Codex wrapper is unavailable.'),
    'Maintained launcher plan refused': ('launcher_plan_refused', 'The maintained launcher plan refused the selected route.'),
    'Native Codex receipt mismatch': ('launcher_receipt_mismatch', 'The native Codex receipt did not match the launch.'),
    'Native receipt mismatch': ('launcher_receipt_mismatch', 'The native launch receipt did not match the requested session.'),
    'Native cwd mismatch': ('launcher_cwd_mismatch', 'The native session working directory did not match the requested cwd.'),
    'Native lifecycle receipt is incomplete': ('launcher_receipt_incomplete', 'The native lifecycle receipt is incomplete.'),
    'No confirmed native execution receipt': ('launcher_receipt_incomplete', 'The launcher exited without a confirmed native execution receipt.'),
    'Managed profile removed': ('launcher_unknown_profile', 'The managed engine profile is not installed on this host.'),
    'Managed engine binary missing': ('launcher_binary_missing', 'The managed engine binary is missing on this host.'),
    'Packaged native engine binary missing': ('launcher_binary_missing', 'The packaged native engine binary is missing on this host.'),
    'Native zcodex binary missing': ('launcher_binary_missing', 'The native zcodex binary is missing on this host.'),
    'API key has expired': ('engine_auth_expired', 'The engine authentication failed or API key has expired.'),
    'This API key has expired': ('engine_auth_expired', 'The engine authentication failed or API key has expired.'),
    'AI.Error: This API key has expired': ('engine_auth_expired', 'The engine authentication failed or API key has expired.'),
    'engine_auth_expired': ('engine_auth_expired', 'The engine authentication failed or API key has expired.'),
}

AUTH_EXPIRED_MESSAGE = 'The engine authentication failed or API key has expired.'

AUTH_EXPIRED_PATTERNS = [
    re.compile(r'api\s*key\s*has\s*expired', re.I),
    re.compile(r'this\s*api\s*key\s*has\s*expired', re.I),
    re.compile(r'create\s*a\s*new\s*api\s*key\s*in\s*opencode\s*console', re.I),
    re.compile(r'ai\.error(?:\.authentication)?:\s*this\s*api\s*key\s*has\s*expired', re.I),
    re.compile(r'ai\.error\.authentication', re.I),
    re.compile(r'invalid[_\s-]?api[_\s-]?key', re.I),
    re.compile(r'incorrect\s*api\s*key', re.I),
    re.compile(r'\b401\b.*(?:unauthorized|authentication)', re.I),
    re.compile(r'(?:unauthorized|authentication\s*failed).*401', re.I),
    re.compile(r'\b401\s+unauthorized\b', re.I),
    re.compile(r'status\s*(?:code)?\s*401\b', re.I),
    re.compile(r'authentication\s*failed', re.I),
    re.compile(r'authenticationerror', re.I),
]


def check_auth_expired_text(text: str) -> bool:
    """Return True if text contains genuine provider authentication failure in an error context."""
    if not text:
        return False
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        # Skip lines that are merely command echoes, goal JSON, or cli args
        if clean.startswith('{"goal":') or clean.startswith('args=') or clean.startswith('command=') or 'message="cli starting"' in clean:
            continue
        if re.match(r'^(?:\[aplexer\]|\s*(?:level=INFO|INFO:))', clean):
            continue
        if clean.startswith('Task:') or clean.startswith('Prompt:') or clean.startswith('Instruction:'):
            continue
        # Must have an error indicator
        has_error_indicator = bool(
            re.search(r'(?:error|failed|failure|exception|unauthorized|denied|invalid|expired|cause=|status\s*401)', clean, re.I)
        )
        if not has_error_indicator:
            continue
        # Check patterns
        for pat in AUTH_EXPIRED_PATTERNS:
            if pat.search(clean):
                if any(x in clean for x in ('Implement Hub intake', 'acceptance criteria', 'YOUR SLICE', 'echo ')):
                    continue
                return True
    return False


def is_auth_error_log_line(line: str) -> bool:
    """Check if a single log line is an auth failure error, ignoring info/arg echoes."""
    if not line:
        return False
    if 'message="cli starting"' in line or 'spawning process' in line or 'level=INFO' in line:
        return False
    if 'level=ERROR' in line or 'cause=' in line or 'Error' in line or '401' in line or 'failed' in line.lower():
        return check_auth_expired_text(line)
    return False


def get_opencode_log_paths() -> list[Path]:
    paths = []
    xdg_data = os.environ.get('XDG_DATA_HOME')
    if xdg_data:
        paths.append(Path(xdg_data) / 'opencode' / 'log' / 'opencode.log')
    paths.extend([
        Path.home() / '.local/share/opencode/log/opencode.log',
        Path('/home/alexey/git/agent-hub/.local/platform-runtime/opencode-data/opencode/log/opencode.log'),
    ])
    return [p for p in paths if p.is_file()]


def capture_log_offsets(engine=None) -> dict[str, int]:
    """Capture file sizes for engine logs prior to launch for precise delta inspection."""
    offsets = {}
    if engine in ('opencode', 'go', None):
        for p in get_opencode_log_paths():
            try:
                offsets[str(p)] = p.stat().st_size
            except Exception:
                pass
    return offsets


def check_engine_auth_expired(raw='', engine=None, cwd=None, task_id=None, launched_at=None, start_offsets=None, lookback_bytes=65536) -> bool:
    """Check whether a launch failed due to provider authentication / expired API key."""
    if check_auth_expired_text(str(raw)):
        return True

    # If raw didn't capture the error directly (e.g. child session exited early and output was withheld),
    # inspect known engine log paths when the target engine is known and modified in the launch window.
    if engine in ('opencode', 'go'):
        now_ts = time.time()
        cutoff = (launched_at - 5.0) if (launched_at is not None) else (now_ts - 60.0)
        target_task = str(task_id) if task_id else None
        target_cwd = str(cwd) if cwd else None

        for log_path in get_opencode_log_paths():
            try:
                stat = log_path.stat()
                if stat.st_mtime < cutoff:
                    continue
                start_offset = 0
                if start_offsets and str(log_path) in start_offsets:
                    start_offset = start_offsets[str(log_path)]
                elif start_offsets and isinstance(start_offsets, (int, float)):
                    start_offset = int(start_offsets)

                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    if start_offset > 0 and start_offset < stat.st_size:
                        f.seek(start_offset)
                    elif stat.st_size > lookback_bytes:
                        f.seek(stat.st_size - lookback_bytes)
                    tail = f.read()

                # Parse lines, indexing runs and finding auth errors
                run_tasks: dict[str, str] = {}
                auth_error_runs: list[tuple[str | None, str]] = []
                for line in tail.splitlines():
                    run_m = re.search(r'\brun=([a-zA-Z0-9_-]+)\b', line)
                    run_id = run_m.group(1) if run_m else None
                    if run_id and ('message="cli starting"' in line or 'spawning process' in line):
                        run_tasks[run_id] = line
                    if is_auth_error_log_line(line):
                        ts_m = re.search(r'timestamp=([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z?)', line)
                        log_time = None
                        if ts_m:
                            try:
                                log_time = dt.datetime.fromisoformat(ts_m.group(1).replace('Z', '+00:00')).timestamp()
                            except Exception:
                                pass
                        if log_time is not None and log_time < cutoff:
                            continue
                        auth_error_runs.append((run_id, line))

                for err_run, err_line in auth_error_runs:
                    if target_task:
                        if err_run and err_run in run_tasks:
                            start_line = run_tasks[err_run]
                            if target_task in start_line:
                                return True
                        elif target_task in err_line:
                            return True
                    elif target_cwd:
                        if err_run and err_run in run_tasks:
                            start_line = run_tasks[err_run]
                            if f'cwd={target_cwd}' in start_line:
                                return True
                        elif target_cwd in err_line:
                            return True
                    else:
                        return True
            except Exception:
                pass

    return False


def retain_per_session_opencode_log(log_offsets, dest_path, max_bytes=65536, engine=None) -> str | None:
    """Retain complete bounded new logs, filtered before any excerpt is taken.

    A saved offset can lie inside a multiline credential, so shared-log deltas
    and oversized files are withheld. The exact native incarnation's private
    hub-session-logs snapshot remains the primary per-session output evidence.
    """
    try:
        from . import private_fs
        from .log_redaction import redact_log
        if engine is not None and engine not in ('opencode', 'go'):
            return None
        if not isinstance(log_offsets, dict) or not log_offsets:
            return None
        if type(max_bytes) is not int or not 0 < max_bytes <= 65536:
            return None
        chunks = []
        used = 0
        for log_path in get_opencode_log_paths():
            try:
                offset = log_offsets.get(str(log_path), 0)
                if type(offset) is not int or offset != 0:
                    continue
                # Read from byte zero only; never discard privacy context.
                with private_fs.parent(log_path, private=False) as anchor:
                    fd = private_fs._open(anchor, log_path, os.O_RDONLY, private=False)
                    with os.fdopen(fd, 'rb') as stream:
                        if os.fstat(stream.fileno()).st_size > max_bytes:
                            continue
                        raw = stream.read(max_bytes + 1)
                if not raw or len(raw) > max_bytes:
                    continue
                chunk = redact_log(raw.decode('utf-8', 'replace')).encode('utf-8')
                if used + len(chunk) > max_bytes:
                    break
                chunks.append(chunk)
                used += len(chunk)
            except (OSError, ValueError):
                continue
        if not used:
            return None
        private_fs.atomic_write(Path(dest_path), b''.join(chunks), max_bytes)
        return str(dest_path)
    except (OSError, ValueError):
        return None


def _redact_secret_shapes(line: str) -> str:
    """Redact token-like values from bounded path/status lines (never fail)."""
    try:
        from .log_redaction import redact_log
        line = redact_log(line)
        line = re.sub(r'sk-[A-Za-z0-9_-]{8,}', 'sk-[redacted]', line)
        line = re.sub(r'(?i)\bsk-ant-[A-Za-z0-9_-]{8,}\b', 'sk-ant-[redacted]', line)
        line = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[redacted]', line)
        line = re.sub(r'(?i)(api[_-]?key\s*[:=]\s*)[^\s;]+', r'\1[redacted]', line)
        line = re.sub(r'(?i)(token\s*[:=]\s*)[^\s;]+', r'\1[redacted]', line)
        line = re.sub(r'gh[pousr]_[A-Za-z0-9_]{16,}', '[redacted]', line)
        line = re.sub(r'github_pat_[A-Za-z0-9_]{16,}', '[redacted]', line)
        line = re.sub(r'\bxox[baprs]-[A-Za-z0-9-]{8,}', '[redacted]', line)
        line = re.sub(r'\bAKIA[0-9A-Z]{16}\b', '[redacted]', line)
    except Exception:
        pass
    return line


CRASH_SIGNATURES = (
    'transport: the socket connection was closed unexpectedly',
    'bun has crashed',
    'panic(',
    'abort() called',
    'heap out of memory',
    'allocation failure',
    'fatal error',
    'traceback (most recent call last)',
    'out of memory',
    'oom killer',
    'sigabrt',
    'sigsegv',
    'core dumped',
)


def _looks_like_crash_line(line: str) -> bool:
    """Fixed-substring crash match on a bounded line (never fail, no regex)."""
    try:
        lowered = str(line).lower()
    except Exception:
        return False
    return any(sig in lowered for sig in CRASH_SIGNATURES)


def extract_crash_tail(screen_text, *, max_lines=3, limit=512) -> str:
    """Bounded sanitized runtime-failure tail from native screen text.

    Keeps only the last matching crash-signature lines (the Bun panic /
    abort / OOM shape, plus the observed socket-closed transport error).
    Sanitized with the runtime tail scrubber so secret shapes
    never survive. Never raises; returns '' when nothing matches.
    """
    try:
        from .runtime_diagnostics import _sanitize_tail
    except Exception:
        _sanitize_tail = None  # type: ignore[assignment]
    try:
        from .log_redaction import redact_log
        raw = str(screen_text or '')
        if len(raw) > 65536:
            return ''
        # Remove multi-line secrets before selecting matching lines, otherwise
        # selection could discard the PEM delimiters needed by the scrubber.
        lines = redact_log(raw).splitlines()
    except Exception:
        return ''
    hits = [ln for ln in lines if _looks_like_crash_line(ln)]
    if not hits:
        return ''
    # Sanitize complete lines before clipping: clipping can remove the label
    # identifying a credential. Failure to sanitize must withhold the tail.
    tail = '\n'.join(ln.strip() for ln in hits[-max_lines:] if ln.strip())
    if _sanitize_tail is not None:
        try:
            return _sanitize_tail(tail, limit)[:limit]
        except Exception:
            pass
    return ''


def read_native_screen_crash_tail(state_dir, native_id, *, max_bytes=16384, limit=512) -> str:
    """Best-effort sanitized crash tail from a native screen.txt snapshot.

    Reads a complete snapshot up to64KiB ONLY from
    <state_dir>/sessions|retired-sessions/<uuid>/screen.txt for a validated
    UUID (never follows symlinks). Filters before limiting the returned tail. Returns the
    crash-signature tail or ''. Never raises, never returns raw screen text.
    """
    try:
        import uuid as _uuid
        norm = str(_uuid.UUID(str(native_id))).lower()
    except Exception:
        return ''
    try:
        from .retired_native import resolve_aplexer_state_dir
        from . import private_fs
        base = resolve_aplexer_state_dir(state_dir)
        import os as _os
        for sub in ('retired-sessions', 'sessions'):
            path = _os.path.join(base, sub, norm, 'screen.txt')
            try:
                if _os.path.islink(path) or not _os.path.isfile(path):
                    continue
                size = _os.path.getsize(path)
                if size <= 0 or size > 65536:
                    continue
                with private_fs.parent(path, private=False) as anchor:
                    fd = private_fs._open(anchor, path, _os.O_RDONLY, private=False)
                    with _os.fdopen(fd, 'rb') as f:
                        size = _os.fstat(f.fileno()).st_size
                        data = f.read(65537)
                        if len(data) > 65536:
                            continue
                text = data.decode('utf-8', 'replace')
            except Exception:
                continue
            tail = extract_crash_tail(text, limit=min(limit, max_bytes))
            if tail:
                return tail
    except Exception:
        pass
    return ''


def _git_evidence(workspace, args, deadline):
    """Read at most 4 KiB within one shared deadline; never expose stderr.

    Commands use an isolated process group so timeout/overflow cleanup also
    covers their own SSH/credential-helper children. No remote URL is read.
    """
    import os
    import selectors
    import signal
    import subprocess
    import time
    if os.name == 'nt' or time.monotonic() >= deadline:
        return None
    process = None
    complete = False
    try:
        process = subprocess.Popen(['git', *args], cwd=workspace,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    return None
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 4096:
                    return None
        code = process.wait(timeout=max(.001, deadline - time.monotonic()))
        complete = True
        return code, data.decode('utf-8', 'replace')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        if process is not None:
            if not complete:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.stdout.close()
            process.wait(timeout=2)


def _verified_pushed_commits(workspace, commits):
    """Positive origin/main containment only; local observation is not a push.

    A remote observation proves where a commit exists, not who authored or
    pushed it. Missing objects, failed auth and timeouts remain unverified.
    The entire optional enrichment shares a three-second budget.
    """
    import time
    deadline = time.monotonic() + 3
    candidates = []
    for line in commits[:5]:
        sha = line.split(' ', 1)[0]
        if not re.fullmatch(r'[0-9a-f]{7,40}', sha):
            continue
        value = _git_evidence(workspace, ['rev-parse', '--verify', sha + '^{commit}'], deadline)
        if value and value[0] == 0 and re.fullmatch(r'[0-9a-f]{40}\n?', value[1]):
            candidates.append(value[1].strip())
    if not candidates:
        return []
    remote = _git_evidence(workspace, ['ls-remote', '--exit-code', 'origin', 'refs/heads/main'], deadline)
    match = re.fullmatch(r'([0-9a-f]{40})\trefs/heads/main\n?', remote[1]) if remote and remote[0] == 0 else None
    if not match:
        return []
    tip = match[1]
    pushed = []
    for sha in candidates:
        ancestor = _git_evidence(workspace, ['merge-base', '--is-ancestor', sha, tip], deadline) if sha != tip else (0, '')
        if ancestor and ancestor[0] == 0:
            pushed.append(sha)
    return pushed


def describe_partial_work(workspace, launched_at_ts=None, max_files=20) -> dict:
    """Bounded local description of uncommitted work + recent commits (paths only)."""
    info: dict = {'dirty': [], 'commits': []}
    try:
        ws = Path(workspace)
        if not ws.is_absolute() or not (ws / '.git').exists():
            return info
    except Exception:
        return info
    try:
        import subprocess as _sp
        out = _sp.run(['git', 'status', '--porcelain=v1', '-uall'], cwd=str(workspace),
                      capture_output=True, timeout=5, text=True)
        if out.returncode == 0:
            for line in _redact_secret_shapes(out.stdout or '').splitlines()[:max_files]:
                line = ''.join(ch if 32 <= ord(ch) < 127 else '?' for ch in line)[:200]
                if line.strip():
                    info['dirty'].append(_redact_secret_shapes(line.strip()))
    except Exception:
        pass
    try:
        import subprocess as _sp
        argv = ['git', 'log', '--oneline', '-5', '--no-decorate']
        try:
            ts = float(launched_at_ts) if launched_at_ts is not None else 0.0
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            try:
                import datetime as _dt
                since = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                argv = ['git', 'log', '--oneline', '-5', '--no-decorate', '--since=' + since]
            except Exception:
                pass
        out = _sp.run(argv, cwd=str(workspace), capture_output=True, timeout=5, text=True)
        if out.returncode == 0:
            for line in _redact_secret_shapes(out.stdout or '').splitlines()[:5]:
                line = ''.join(ch if 32 <= ord(ch) < 127 else '?' for ch in line)[:200]
                if line.strip():
                    info['commits'].append(_redact_secret_shapes(line.strip()))
    except Exception:
        pass
    if info['commits']:
        try:
            info['pushed_commits'] = _verified_pushed_commits(str(workspace), info['commits'])
        except Exception:
            # Optional remote provenance must never erase local partial work.
            info['pushed_commits'] = []
    return info


def _sanitize_cli_error_line(line: str) -> str | None:
    """Return a bounded, secret/path-redacted native launcher CLI error line."""
    s = ''.join(ch for ch in line.strip() if 32 <= ord(ch) < 127)
    if not re.match(r'^(?:a|aplexer|systemd-run): ', s):
        return None
    s = _redact_secret_shapes(s)
    s = re.sub(r'\b[A-Za-z0-9_.-]+=\S+', '[redacted]', s)
    s = re.sub(r'(?<!\w)/[A-Za-z0-9._/-]+', '[path]', s)
    return s[:220]


def diagnostic(raw='', *, error_type='RuntimeError', exit_code=None, stage='launcher_run', engine=None, cwd=None, task_id=None, launched_at=None, start_offsets=None):
    reason, message = 'launcher_command_failed', 'The launcher command failed; unrecognized output was withheld.'
    refusal_found = False
    surfaced_tail = None

    # Only complete known messages or sanitized launcher CLI diagnostics cross the privacy boundary.
    raw_str = str(raw or '')
    bounded_wait_expired = raw_str.strip() == 'Local command failed or exceeded resource limit'
    for line in raw_str[-16384:].splitlines():
        clean_line = line.strip()
        typed = re.match(r'^(ValueError|RuntimeError|PermissionError|OSError|FileNotFoundError|TimeoutError|JSONDecodeError): ', clean_line)
        if typed:
            error_type = typed.group(1)
        candidate = re.sub(r'^(?:ValueError|RuntimeError|PermissionError|OSError|FileNotFoundError|TimeoutError|JSONDecodeError): ', '', clean_line)
        # Current launcher resource policy uses a configurable MiB floor.
        # Accept only its complete numeric grammar, never arbitrary exception text.
        # Intake 63a9ee3a: newer launchers append the numeric
        # available/reserved/required/floor breakdown.
        if re.fullmatch(r'host MemAvailable below [0-9]{1,7}MiB floor after reservation \(launcher_memory_reserve\)'
                        r'(?:: available_mb=[0-9]{1,9} reserved_mb=[0-9]{1,9} required_mb=[0-9]{1,9} floor_mb=[0-9]{1,7})?', candidate):
            reason, message = REFUSALS['launcher_memory_reserve']
            surfaced_tail = error_type + ': ' + candidate
            refusal_found = True
            break
        # The maintained launcher emits this numeric ValueError before spawn.
        # Match the entire grammar; never forward raw paths, suffixes or payloads.
        if re.fullmatch(r'(?:cwd|tmpdir) filesystem free < (?:20|50)GiB floor \+ required disk \([0-9]{1,16} B\)', candidate):
            reason, message = REFUSALS['launcher_disk_reserve']
            surfaced_tail = error_type + ': ' + candidate
            refusal_found = True
            break
        if candidate in REFUSALS:
            reason, message = REFUSALS[candidate]
            refusal_found = True
            break
        elif candidate == 'Unsupported provider: codex (genuine protected wrapper scripts/launch-codex.sh not configured; raw routing denied)':
            reason, message = 'launcher_codex_wrapper_unavailable', 'The protected Codex wrapper is not configured in the launcher environment.'
            refusal_found = True
            break
        elif re.fullmatch(r'(?:a|aplexer): unknown profile [A-Za-z0-9][A-Za-z0-9._-]{0,63}', candidate):
            reason, message = 'launcher_unknown_profile', f'The native launcher refused start: {candidate}.'
            surfaced_tail = candidate
            refusal_found = True
            break
        elif re.fullmatch(r'(?:a|aplexer): unknown engine [A-Za-z0-9][A-Za-z0-9._-]{0,63}', candidate):
            reason, message = 'launcher_unknown_engine', f'The native launcher refused start: {candidate}.'
            surfaced_tail = candidate
            refusal_found = True
            break
        elif candidate.startswith('model_mismatch:'):
            reason, message = 'launcher_model_mismatch', 'The native session model did not match the requested model.'
            refusal_found = True
            break
        elif candidate.startswith('model_unsupported:'):
            reason, message = 'launcher_model_unsupported', 'The requested model is not supported by the host launcher route.'
            refusal_found = True
            break
        elif candidate.startswith('{') and candidate.endswith('}'):
            try:
                parsed_json = json.loads(candidate)
            except Exception:
                parsed_json = None
            if isinstance(parsed_json, dict) and parsed_json.get('launch_allowed') is False and isinstance(parsed_json.get('reason'), str):
                reason, message = 'launcher_quota_gate_refused', 'The host Codex quota gate refused launch before execution.'
                refusal_found = True
                break
        cli_line = _sanitize_cli_error_line(candidate)
        if cli_line is not None:
            reason, message = 'launcher_command_failed', f'The launcher command failed: {cli_line}'
            surfaced_tail = cli_line
            refusal_found = True
            break

    # If no exact known refusal was present in raw output, check for auth failure in raw or launch-correlated log
    if not refusal_found:
        if check_auth_expired_text(raw_str):
            reason, message = 'engine_auth_expired', AUTH_EXPIRED_MESSAGE
        elif check_engine_auth_expired(raw_str, engine=engine, cwd=cwd, task_id=task_id, launched_at=launched_at, start_offsets=start_offsets):
            reason, message = 'engine_auth_expired', AUTH_EXPIRED_MESSAGE

    if error_type not in {'ValueError', 'RuntimeError', 'OSError', 'PermissionError', 'FileNotFoundError', 'TimeoutError', 'JSONDecodeError'}:
        error_type = 'RuntimeError'
    if (reason == 'launcher_command_failed'
            and message == 'The launcher command failed; unrecognized output was withheld.'):
        # Intake 643395c0: name the bounded receipt-wait expiry and carry the
        # exit code instead of a bare withheld claim. Messages stay fixed
        # constants; raw launcher bytes are never forwarded.
        if bounded_wait_expired:
            message = 'The bounded launcher receipt wait expired before a confirmed receipt; any native effect is reconciled separately.'
        elif type(exit_code) is int:
            message = 'The launcher command failed with exit code %d; unrecognized output withheld.' % exit_code
    value = {'reason': reason, 'message': message, 'error_type': error_type,
             'stage': stage if stage in {'launcher_run', 'dispatch_headless', 'dispatch_interactive', 'dispatch_route'} else 'launcher_run'}
    if surfaced_tail is not None:
        value['tail'] = surfaced_tail
    if reason in {'cpu_headroom', 'cpu_pressure', 'memory_pressure', 'host_oom', 'host_runtime_choking'}:
        from .cpu_resources import sanitize_cpu_admission
        for line in raw_str[-16384:].splitlines():
            if line.startswith('CPU_ADMISSION_JSON ') and len(line) <= 2048:
                try:
                    cpu = sanitize_cpu_admission(json.loads(line[len('CPU_ADMISSION_JSON '):]))
                except (ValueError, TypeError):
                    cpu = None
                if cpu is not None:
                    value['cpu'] = cpu
                    break
    if type(exit_code) is int:
        value['exit_code'] = exit_code
    return value


class LocalCommandError(RuntimeError):
    def __init__(self, raw='', *, exit_code=None, command=None, stdout=None, stderr='', reconcile_effects=False, engine=None, cwd=None, task_id=None, launched_at=None, start_offsets=None):
        from .command_diagnostics import command_details
        self.diagnostic = diagnostic(raw, exit_code=exit_code, engine=engine, cwd=cwd, task_id=task_id, launched_at=launched_at, start_offsets=start_offsets)
        # Raw exceptions are classification input, not captured command output.
        self.command_diagnostic = command_details(command, stdout, stderr)
        self.diagnostic.update(self.command_diagnostic)
        if (self.diagnostic.get('reason') == 'launcher_command_failed'
                and isinstance(raw, (str, bytes)) and not raw.strip()
                and stdout is not None and stderr is not None
                and not self.command_diagnostic['stdout_tail']
                and not self.command_diagnostic['stderr_tail']
                and type(exit_code) is int):
            self.diagnostic['message'] = ('The launcher returned exit code %d without stdout or stderr; '
                                          'no native receipt was captured.' % exit_code)
        if reconcile_effects:
            self.diagnostic['message'] = 'Native client operation failed; reconcile effects'
        super().__init__(self.diagnostic['message'])


def exception_diagnostic(exc, stage, engine=None, cwd=None, task_id=None, launched_at=None, start_offsets=None):
    existing = getattr(exc, 'diagnostic', None)
    if isinstance(existing, dict):
        diag = {**existing, 'stage': stage}
        if diag.get('reason') == 'launcher_command_failed' and check_engine_auth_expired(str(exc), engine=engine, cwd=cwd, task_id=task_id, launched_at=launched_at, start_offsets=start_offsets):
            diag['reason'] = 'engine_auth_expired'
            diag['message'] = AUTH_EXPIRED_MESSAGE
        return diag
    return diagnostic(str(exc), error_type=type(exc).__name__, stage=stage, engine=engine, cwd=cwd, task_id=task_id, launched_at=launched_at, start_offsets=start_offsets)
