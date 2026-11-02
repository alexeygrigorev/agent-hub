"""Read Antigravity's native, read-only ``/usage`` view for one exact HOME."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess

TOKEN_PATH = Path('.gemini/antigravity-cli/antigravity-oauth-token')
MAX_OUTPUT = 32768
MAX_SECONDS = 20
WINDOWS = {
    'Five Hour Limit Remaining': '5h',
    'Weekly Limit Remaining': '7d',
}


def _reset(value, now):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid_reset')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('invalid_reset')
    parsed = parsed.astimezone(dt.timezone.utc)
    if parsed <= now:
        raise ValueError('stale_reset')
    return parsed.isoformat()


def parse_usage(payload, *, now=None):
    """Convert official ``agy -p /usage --output-format json`` output to quse shape.

    The response is tab-separated by the CLI. Only the Gemini quota group is
    relevant to the Antigravity route; the separate Claude/GPT group is ignored.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('invalid_clock')
    now = now.astimezone(dt.timezone.utc)
    if not isinstance(payload, dict) or payload.get('status') != 'SUCCESS' or payload.get('error'):
        raise ValueError('usage_unavailable')
    response = payload.get('response')
    if not isinstance(response, str) or len(response.encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('invalid_usage')

    windows = {}
    for line in response.splitlines():
        columns = line.split('\t')
        if len(columns) != 4:
            continue
        group, label, percentage, reset = columns
        if group != 'Gemini Models' or label not in WINDOWS:
            continue
        match = re.fullmatch(r'(\d{1,3}(?:\.\d{1,4})?)%', percentage)
        if not match:
            raise ValueError('invalid_percentage')
        remaining = float(match.group(1))
        if not math.isfinite(remaining) or not 0 <= remaining <= 100:
            raise ValueError('invalid_percentage')
        key = WINDOWS[label]
        if key in windows:
            raise ValueError('duplicate_window')
        windows[key] = {'percent_remaining': remaining, 'reset_at': _reset(reset, now)}
    if not windows:
        raise ValueError('gemini_quota_missing')
    return {'gemini': {
        'status': 'ok',
        'error': None,
        'details': {'source': 'native_antigravity_usage', 'observed_at': now.isoformat(),
                    'limit_reached': any(w['percent_remaining'] == 0 for w in windows.values())},
        'windows': windows,
    }}


def _credential_present(home):
    """Require the account-specific native credential before invoking agy."""
    home = Path(home)
    path = home / TOKEN_PATH
    try:
        current = path
        while current != home and home in current.parents:
            if current.is_symlink():
                return False
            current = current.parent
        if current.is_symlink() or path.is_symlink() or not path.is_file() or not 2 <= path.stat().st_size <= 32768:
            return False
        data = json.loads(path.read_text(encoding='utf-8'))
        token = data.get('token') if isinstance(data, dict) else None
        return (isinstance(token, dict) and isinstance(token.get('access_token'), str)
                and bool(token['access_token']) and isinstance(token.get('refresh_token'), str)
                and bool(token['refresh_token']))
    except Exception:
        return False


CREATE_NO_WINDOW = 0x08000000
_CLI_NAMES = {'agy', 'agy.exe', 'agy.cmd', 'agy.bat'}


def valid_cli(binary):
    """True only for the ``agy`` CLI. The Antigravity desktop app (Antigravity.exe,
    or the ``antigravity`` editor launcher) never returns from ``-p`` and leaks."""
    if not binary:
        return False
    text = str(binary).replace(chr(92), '/')
    parts = [p.lower() for p in text.split('/') if p]
    if not parts or parts[-1] not in _CLI_NAMES:
        return False
    return 'antigravity' not in parts[:-1]


def kill_tree(proc):
    """Kill proc and every descendant (taskkill /T /F on Windows, process group on POSIX)."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=CREATE_NO_WINDOW, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_bounded(argv, *, env, timeout):
    """Run argv with a hard timeout; the whole process tree is always reaped."""
    kwargs = {'creationflags': CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, **kwargs)
    try:
        out, _ = proc.communicate(timeout=timeout)
        if proc.returncode != 0 or len(out) > MAX_OUTPUT:
            return None
        return json.loads(out.decode('utf-8'))
    finally:
        # Also covers success: reap any child the CLI left in its group/tree.
        if proc.poll() is None or os.name != 'nt':
            kill_tree(proc)


def probe(binary, home, *, runner=None, now=None):
    """Probe via Antigravity's own no-model ``/usage`` command, pinned to HOME."""
    now = now or dt.datetime.now(dt.timezone.utc)
    empty = {'gemini': {'status': 'unknown', 'error': 'native_antigravity_unavailable',
                        'details': {'source': 'native_antigravity_usage', 'observed_at': now.isoformat()},
                        'windows': {}}}
    home = Path(home)
    if not valid_cli(binary) or not home.is_absolute() or not _credential_present(home):
        return empty
    try:
        argv = [str(binary), '-p', '/usage', '--output-format', 'json']
        env = {'HOME': str(home), 'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8',
               'TERM': 'dumb', 'CI': '1', 'NO_COLOR': '1'}
        if runner is None:
            payload = run_bounded(argv, env=env, timeout=MAX_SECONDS)
            if payload is None:
                return empty
        else:
            payload = runner(argv, env=env, timeout=MAX_SECONDS)
        return parse_usage(payload, now=now)
    except Exception:
        # CLI errors may include provider text or profile paths. Never expose them.
        return empty
