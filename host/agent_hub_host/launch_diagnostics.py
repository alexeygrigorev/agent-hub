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
    'memory_pressure': ('memory_pressure', 'Host memory full pressure exceeds the safety cap.'),
    'host_oom': ('host_oom', 'A recent host OOM kill requires a placement cooldown.'),
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
    'Native lifecycle receipt is incomplete': ('launcher_receipt_incomplete', 'The native lifecycle receipt is incomplete.'),
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


def diagnostic(raw='', *, error_type='RuntimeError', exit_code=None, stage='launcher_run', engine=None, cwd=None, task_id=None, launched_at=None, start_offsets=None):
    reason, message = 'launcher_command_failed', 'The launcher command failed; unrecognized output was withheld.'
    refusal_found = False

    # Only complete known messages may be copied across the privacy boundary.
    raw_str = str(raw or '')
    for line in raw_str[-16384:].splitlines():
        clean_line = line.strip()
        typed = re.match(r'^(ValueError|RuntimeError|PermissionError|OSError|FileNotFoundError|TimeoutError|JSONDecodeError): ', clean_line)
        if typed:
            error_type = typed.group(1)
        candidate = re.sub(r'^(?:ValueError|RuntimeError|PermissionError|OSError|FileNotFoundError|TimeoutError|JSONDecodeError): ', '', clean_line)
        if candidate in REFUSALS:
            reason, message = REFUSALS[candidate]
            refusal_found = True
            break
        elif candidate == 'Unsupported provider: codex (genuine protected wrapper scripts/launch-codex.sh not configured; raw routing denied)':
            reason, message = 'launcher_codex_wrapper_unavailable', 'The protected Codex wrapper is not configured in the launcher environment.'
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
    value = {'reason': reason, 'message': message, 'error_type': error_type,
             'stage': stage if stage in {'launcher_run', 'dispatch_headless', 'dispatch_interactive', 'dispatch_route'} else 'launcher_run'}
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
    def __init__(self, raw='', *, exit_code=None, engine=None, cwd=None, task_id=None, launched_at=None, start_offsets=None):
        self.diagnostic = diagnostic(raw, exit_code=exit_code, engine=engine, cwd=cwd, task_id=task_id, launched_at=launched_at, start_offsets=start_offsets)
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
