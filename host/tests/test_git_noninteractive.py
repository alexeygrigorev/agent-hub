import subprocess
from types import SimpleNamespace

import pytest

from agent_hub_host import git_noninteractive as gn
from agent_hub_host import repo_workspace


def test_env_never_prompts_and_resets_selector_on_windows():
    env = gn.noninteractive_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"},
                                platform="win32", gh_bin="C:\Program Files\GitHub CLI\gh.exe")
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("credential.helper", "")
    assert env["GIT_CONFIG_VALUE_2"] == '!"C:/Program Files/GitHub CLI/gh.exe" auth git-credential'
    assert env["GIT_CONFIG_KEY_0"] == "a.b"


def test_no_gh_still_resets_helper_and_linux_keeps_credentials():
    env = gn.noninteractive_env({}, platform="win32", gh_bin="")
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_VALUE_0"] == ""
    assert "GIT_CONFIG_COUNT" not in gn.noninteractive_env({}, platform="linux")
    assert gn.session_env_pairs(platform="linux") == []
    assert ("GCM_INTERACTIVE", "never") in gn.session_env_pairs(platform="win32", gh_bin="gh")


def test_clone_auth_failure_has_clear_reason(tmp_path):
    def runner(argv, cwd, timeout):
        return SimpleNamespace(returncode=128, stdout="", stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled")
    label = "repo-0123abcd-DataTalksClub__demo"
    with pytest.raises(ValueError, match="^repo_clone_auth_failed: DataTalksClub/demo"):
        repo_workspace.ensure_repo_workspace({"work_root": str(tmp_path)}, label, runner=runner, gh_bin="")
