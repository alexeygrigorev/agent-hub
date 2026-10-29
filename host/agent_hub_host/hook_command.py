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
