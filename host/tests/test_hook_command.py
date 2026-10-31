"""Generated hook commands must survive bash on Windows (intake 50f28bc1).

Claude Code on Windows runs hook commands through Git Bash. A raw native path
like C:\\Users\\alexe\\.local\\bin\\a.EXE loses its backslashes there
(`C:Usersalexe.localbina.EXE: command not found`), so the Stop hook never
reported idle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import hook_command as hc
from agent_hub_host import private_fs
from agent_hub_host import provisioning as p

REPO_TMP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))
WIN_A = "C:\\Users\\alexe\\.local\\bin\\a.EXE"


def _bash_exe():
    """Locate the bash that would actually run engine hooks on this host.

    Claude Code resolves Git Bash itself on Windows; a pytest process spawned
    from a Git Bash session inherits a POSIX-formatted PATH that Windows
    ``subprocess`` cannot search, so fall back to the override Claude uses and
    then the standard Git for Windows install locations.
    """
    override = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if override and os.path.isfile(override):
        return override
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                      r"C:\Program Files\Git\usr\bin\bash.exe",
                      r"C:\Program Files (x86)\Git\bin\bash.exe",
                      r"C:\Program Files (x86)\Git\usr\bin\bash.exe"):
        if os.path.isfile(candidate):
            return candidate
    pytest.skip("no bash available to verify hook word splitting")


def _bash_words(command):
    """What bash would pass as argv for ``command`` (without running it)."""
    out = subprocess.run([_bash_exe(), "-c", 'printf "%s\\n" ' + command],
                         capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_windows_path_becomes_forward_slashes():
    assert hc.shell_path(WIN_A, windows=True) == "C:/Users/alexe/.local/bin/a.EXE"
    # Detected from the drive letter even when not running on Windows.
    assert hc.shell_path(WIN_A, windows=False) == "C:/Users/alexe/.local/bin/a.EXE"


def test_posix_path_unchanged():
    assert hc.shell_path("/home/alexey/.local/bin/a", windows=False) == "/home/alexey/.local/bin/a"


def test_path_with_spaces_is_quoted_and_survives_bash():
    rendered = hc.shell_path("C:\\Users\\Jane Doe\\.local\\bin\\a.EXE", windows=True)
    assert rendered == '"C:/Users/Jane Doe/.local/bin/a.EXE"'
    assert _bash_words(rendered) == ["C:/Users/Jane Doe/.local/bin/a.EXE"]


def test_bash_keeps_windows_hook_path_intact():
    command = hc.state_report_command(hc.shell_path(WIN_A, windows=True), "idle")
    assert command == "C:/Users/alexe/.local/bin/a.EXE state-report idle || true"
    words = _bash_words(command.split(" || ")[0])
    assert words == ["C:/Users/alexe/.local/bin/a.EXE", "state-report", "idle"]
    # The old raw form is exactly what bash mangled.
    assert _bash_words(WIN_A)[0] == "C:Usersalexe.localbina.EXE"


def test_tool_command_uses_resolved_windows_path():
    with mock.patch.object(hc.shutil, "which", return_value=WIN_A):
        assert hc.tool_command("a", windows=True) == "C:/Users/alexe/.local/bin/a.EXE"


def test_repair_rewrites_only_generated_state_report_hooks():
    a_cmd = "C:/Users/alexe/.local/bin/a.EXE"
    old = f"{WIN_A} state-report idle || true"
    assert hc.repair_state_report_command(old, a_cmd) == f"{a_cmd} state-report idle || true"
    assert hc.repair_state_report_command(
        "/home/x/.local/bin/a state-report working || true", a_cmd) == f"{a_cmd} state-report working || true"
    for custom in ("/usr/bin/custom-stop-hook",
                   "/usr/bin/other state-report idle || true",
                   f"{WIN_A} state-report idle; echo extra"):
        assert hc.repair_state_report_command(custom, a_cmd) == custom


def test_claude_managed_home_on_windows_gets_bash_safe_hooks_and_repairs_old_ones():
    os.makedirs(REPO_TMP, exist_ok=True)
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="hook-cmd-")
    try:
        home = os.path.join(base, "managed-home")
        claude_dir = os.path.join(home, ".claude")
        broken = {"hooks": [{"command": f"{WIN_A} state-report idle || true", "type": "command"}]}
        custom = {"hooks": [{"command": "/usr/bin/custom-hook", "type": "command"}]}
        # Seed the file the way provisioning itself does: private_fs writes keep
        # the owner-only DACL Windows read-back verification requires (a plain
        # open()+chmod leaves inherited ACLs that private_fs rightly rejects).
        settings_path = os.path.join(claude_dir, "settings.json")
        private_fs.atomic_write(
            settings_path,
            json.dumps({"hooks": {"Stop": [broken], "PreToolUse": [custom]}}).encode(),
            65536)
        with mock.patch.object(hc.shutil, "which", return_value=WIN_A):
            p.install_managed_engine_permissions(
                bundle={"provider": "claude"}, home=home, trusted_paths=[])
        hooks = json.load(open(os.path.join(claude_dir, "settings.json")))["hooks"]
        commands = [h["command"] for event in ("SessionStart", "UserPromptSubmit", "Stop")
                    for g in hooks[event] for h in g["hooks"]]
        assert commands == [
            "C:/Users/alexe/.local/bin/a.EXE state-report working || true",
            "C:/Users/alexe/.local/bin/a.EXE state-report working || true",
            "C:/Users/alexe/.local/bin/a.EXE state-report idle || true",
        ]
        assert all("\\" not in c for c in commands)
        assert hooks["PreToolUse"] == [custom]
    finally:
        shutil.rmtree(base, ignore_errors=True)
