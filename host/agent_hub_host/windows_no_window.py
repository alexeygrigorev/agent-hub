"""Keep Hub host-controller child processes from opening console windows.

Alexey (intake 446227dc): "when you launch agents on windows make sure the
terminal window doesn't pop up because it's distracting."

A Windows host controller runs without a visible console, so every console
child it spawns (git, npm, uv, ssh, powershell, aplexer ``a``) would otherwise
get its own flashing window. ``install()`` makes ``subprocess.Popen`` (and so
``run``, ``check_output`` and asyncio subprocesses) add ``CREATE_NO_WINDOW``
and a hidden ``STARTUPINFO`` by default. It is called only by the long-running
host commands, never by interactive CLI use, where children must keep the
user's console.
"""
from __future__ import annotations

import subprocess
import sys

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
_MARK = '_agent_hub_no_window'


def hidden_flags(flags=0):
    """Return creationflags that never produce a visible console window."""
    flags = int(flags or 0)
    if flags & CREATE_NEW_CONSOLE:
        flags &= ~CREATE_NEW_CONSOLE
    if not flags & DETACHED_PROCESS:
        flags |= CREATE_NO_WINDOW
    return flags


def hidden_startupinfo(info=None):
    """Return a STARTUPINFO with SW_HIDE set (copy-free: caller's object wins)."""
    if info is None:
        info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def install(platform=None):
    """Patch ``subprocess.Popen`` once. Returns True if active, False on non-Windows."""
    if (platform or sys.platform) != 'win32':
        return False
    original = subprocess.Popen.__init__
    if getattr(original, _MARK, False):
        return True

    def __init__(self, *args, **kwargs):
        kwargs['creationflags'] = hidden_flags(kwargs.get('creationflags', 0))
        kwargs['startupinfo'] = hidden_startupinfo(kwargs.get('startupinfo'))
        original(self, *args, **kwargs)

    setattr(__init__, _MARK, True)
    subprocess.Popen.__init__ = __init__
    return True
