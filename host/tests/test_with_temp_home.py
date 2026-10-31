"""Incident 504d662b: a disposable HOME must never lead to removing the real home."""
from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

import pytest

from agent_hub_host.session_rules import SESSION_PREAMBLE, TEMP_HOME_RULE, compose_prompt

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "with-temp-home.sh"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def run(*args, **kw):
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=60, **kw)


def test_rule_is_in_every_session_preamble():
    assert TEMP_HOME_RULE in SESSION_PREAMBLE
    assert TEMP_HOME_RULE in compose_prompt("brief")
    assert "scripts/with-temp-home.sh" in TEMP_HOME_RULE
    assert "~/.local/bin" in TEMP_HOME_RULE


def test_command_sees_disposable_home_and_it_is_removed(tmp_path):
    env = dict(os.environ, TMPDIR=str(tmp_path))
    res = run("--", "sh", "-c", 'echo "$HOME"; touch "$HOME/x"', env=env)
    assert res.returncode == 0, res.stderr
    used = Path(res.stdout.strip())
    assert used != REAL_HOME and str(used).startswith(str(tmp_path))
    assert not used.parent.exists()
    assert REAL_HOME.is_dir()


def test_exit_code_is_preserved(tmp_path):
    res = run("--", "sh", "-c", "exit 7", env=dict(os.environ, TMPDIR=str(tmp_path)))
    assert res.returncode == 7


@pytest.mark.parametrize("target", [
    "/", "/home", str(REAL_HOME), str(REAL_HOME) + "/", str(REAL_HOME / ".local"),
    str(REAL_HOME / ".local/bin"), str(REAL_HOME / ".local/lib"), str(REAL_HOME / "prod"),
    str(REAL_HOME / "git"), "",
])
def test_guard_refuses_protected_paths(target):
    res = run("--check-remove", target)
    assert res.returncode != 0
    assert "refuse" in res.stderr


def test_guard_refuses_unmarked_lookalike(tmp_path):
    fake = tmp_path / "with-temp-home.abcdefgh"
    fake.mkdir()
    assert run("--check-remove", str(fake)).returncode != 0
    (fake / ".with-temp-home").touch()
    assert run("--check-remove", str(fake)).returncode == 0
    assert fake.exists()


def test_tmpdir_equal_to_real_home_is_not_used_as_temp_base():
    res = run("--", "sh", "-c", 'echo "$HOME"', env=dict(os.environ, TMPDIR=str(REAL_HOME)))
    assert res.returncode == 0, res.stderr
    assert not res.stdout.strip().startswith(str(REAL_HOME) + "/with-temp-home.")
