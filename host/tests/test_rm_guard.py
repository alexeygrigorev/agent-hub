"""Incident 504d662b: the agent rm guard refuses recursive removal of the home and critical dirs."""
from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "ops" / "guard-bin" / "rm"
HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def rm(*args, cwd=None):
    return subprocess.run([str(GUARD), *args], capture_output=True, text=True, timeout=30, cwd=cwd)


@pytest.mark.parametrize("target", [
    "/", "/home", str(HOME), str(HOME) + "/", str(HOME) + "/.", str(HOME / ".local"),
    str(HOME / ".local/bin"), str(HOME / ".local/lib"), str(HOME / ".local/state"),
    str(HOME / "prod"), str(HOME / "git"), str(HOME / ".config"), str(HOME / ".ssh"),
    str(HOME / ".local/bin") + "/..", "/home/someoneelse",
])
def test_check_refuses_protected(target):
    res = rm("--guard-check", target)
    assert res.returncode == 1 and "refuse" in res.stdout, (target, res)


def test_recursive_rm_of_home_is_refused_without_calling_real_rm():
    res = rm("-rf", "--", str(HOME))
    assert res.returncode == 1 and "rm-guard" in res.stderr
    assert (HOME).is_dir()


@pytest.mark.parametrize("flags", [["-rf"], ["-fr"], ["-R"], ["--recursive", "-f"], ["-r", "-f"]])
def test_every_recursive_spelling_is_guarded(flags):
    res = rm(*flags, str(HOME / ".local/bin"))
    assert res.returncode == 1 and "rm-guard" in res.stderr


def test_symlink_to_home_is_refused(tmp_path):
    link = tmp_path / "h"
    link.symlink_to(HOME)
    assert rm("--guard-check", str(link)).returncode == 1


def test_relative_dotdot_resolving_to_home_is_refused(tmp_path):
    assert rm("--guard-check", "..", cwd=str(HOME / ".local")).returncode == 1


def test_temp_dir_removal_still_works(tmp_path):
    d = tmp_path / "scratch" / "x"
    d.mkdir(parents=True)
    (d / "f").write_text("1")
    res = rm("-rf", str(tmp_path / "scratch"))
    assert res.returncode == 0, res.stderr
    assert not (tmp_path / "scratch").exists()


def test_non_recursive_rm_passes_through(tmp_path):
    f = tmp_path / "f"
    f.write_text("1")
    assert rm("-f", str(f)).returncode == 0 and not f.exists()


def test_main_checkout_under_git_is_refused(tmp_path):
    main = HOME / "git" / "agent-hub"
    if not (main / ".git").is_dir():
        pytest.skip("no main checkout on this host")
    assert rm("--guard-check", str(main)).returncode == 1
