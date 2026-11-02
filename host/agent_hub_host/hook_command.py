"""Shell-safe executable paths for generated engine hook commands.

Claude Code and Codex run hook ``command`` strings through a POSIX shell, and
on Windows that shell is Git Bash (``/usr/bin/bash``). An unquoted native path
such as ``C:\\Users\\alexe\\.local\\bin\\a.EXE`` loses every backslash to
bash escaping (``C:Usersalexe.localbina.EXE: command not found``), so the
aplexer state-report hook silently never fires. Generated hooks therefore use
forward slashes, which both Git Bash and Windows accept, and quote the path
whenever it contains anything outside a conservative safe set.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_SAFE = re.compile(r'^[A-Za-z0-9_@%+=:,./-]+$')
# Our own state-report hook: `<a executable> state-report <state> || true`.
_STATE_REPORT = re.compile(
    r'^(?P<exe>"[^"]*"|\'[^\']*\'|\S+) state-report (?P<state>working|idle|waiting) \|\| true$')


def shell_path(path: str, *, windows: bool | None = None) -> str:
    """Return ``path`` as a single bash word that survives Git Bash on Windows."""
    if windows is None:
        windows = os.name == 'nt'
    text = str(path)
    if windows or re.match(r'^[A-Za-z]:\\', text):
        text = text.replace('\\', '/')
    if _SAFE.match(text):
        return text
    # Double quotes keep Windows-friendly behaviour; escape what bash expands.
    return '"' + re.sub(r'(["\\$`])', r'\\\1', text) + '"'


def tool_command(name: str, *, windows: bool | None = None) -> str:
    """Resolve an aplexer CLI (``a``/``aplexer``) and render it for a hook."""
    found = shutil.which(name) or str(Path.home() / '.local' / 'bin' / name)
    return shell_path(found, windows=windows)


def state_report_command(a_cmd: str, state: str) -> str:
    return f"{a_cmd} state-report {state} || true"


def repair_state_report_command(command: str, a_cmd: str) -> str:
    """Rewrite a previously generated state-report hook to use ``a_cmd``.

    Only our exact generated shape is touched, and only when its executable
    is the aplexer ``a`` binary, so custom user hooks are left alone.
    """
    if not isinstance(command, str):
        return command
    m = _STATE_REPORT.match(command.strip())
    if not m:
        return command
    exe = m.group('exe').strip('"\'')
    base = re.split(r'[\\/]', exe)[-1].lower()
    if base not in ('a', 'a.exe'):
        return command
    return state_report_command(a_cmd, m.group('state'))


AWARENESS_MARKER = 'aplexer-managed-awareness-hook-v1'
AWARENESS_THROTTLE_S = 5
_THROTTLE_MARK = 'aplexer-awareness-throttle-v1'


def throttle_awareness_command(command: str, seconds: int = AWARENESS_THROTTLE_S) -> str:
    """Prefix a PostToolUse awareness hook with a pure-bash per-workspace throttle.

    The hook fires on every tool call and each fire costs a shell plus an
    ``aplexer`` process. Within ``seconds`` of the last run in the same working
    directory the wrapper drains stdin and exits 0 without spawning anything
    (bash builtins only); awareness already suppresses unchanged fingerprints,
    so only new context is delayed, by at most ``seconds``. Non-bash shells run
    the hook unthrottled.
    """
    if AWARENESS_MARKER not in command or _THROTTLE_MARK in command:
        return command
    guard = (
        'if [ -n "$BASH_VERSION" ]; then '
        'f="${TMPDIR:-/tmp}/aplexer-ctx-hook-$UID-${PWD//[^A-Za-z0-9]/_}"; '
        "printf -v n '%(%s)T' -1; read -r l < \"$f\" 2>/dev/null; "
        f'if [ "${{l:-0}}" -gt "$((n-{int(seconds)}))" ] 2>/dev/null; then cat >/dev/null; exit 0; fi; '
        'echo "$n" > "$f" 2>/dev/null; fi; '
        f'# {_THROTTLE_MARK}\n'
    )
    return guard + command


def throttle_post_tool_use_hooks(template: dict, seconds: int = AWARENESS_THROTTLE_S) -> dict:
    """Return ``template`` with awareness commands under PostToolUse throttled (copy)."""
    import copy
    out = copy.deepcopy(template)
    for group in (out.get('hooks') or {}).get('PostToolUse') or []:
        for hook in (group.get('hooks') or []) if isinstance(group, dict) else []:
            if isinstance(hook, dict) and isinstance(hook.get('command'), str):
                hook['command'] = throttle_awareness_command(hook['command'], seconds)
    return out
