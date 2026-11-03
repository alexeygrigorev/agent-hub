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


def test_repository_not_found_is_not_mislabeled_as_auth_failure():
    assert gn.is_repo_not_found('remote: Repository not found.')
    assert not gn.is_auth_failure('remote: Repository not found.')
    assert gn.is_auth_failure('fatal: Authentication failed')


def test_dead_loopback_proxy_is_named_and_bypassed_by_no_proxy():
    def refused(host, port):
        raise ConnectionRefusedError(111, 'refused')
    env = {"HTTPS_PROXY": "http://127.0.0.1:8899", "NO_PROXY": "example.test"}
    assert gn.dead_proxy_finding(env, opener=refused) == "proxy 127.0.0.1:8899 unreachable (ConnectionRefusedError)"
    env["NO_PROXY"] = "github.com"
    assert gn.dead_proxy_finding(env, opener=refused) is None


def test_live_proxy_and_absent_proxy_report_nothing():
    assert gn.dead_proxy_finding({}) is None
    assert gn.dead_proxy_finding({"HTTPS_PROXY": "http://proxy.internal:8080"}, opener=lambda h, p: (_ for _ in ()).throw(AssertionError('must not probe remote'))) is None


def test_gh_object_and_api_hosts_join_the_bypass_check():
    def refused(host, port):
        raise ConnectionRefusedError(111, 'refused')
    env = {"HTTPS_PROXY": "http://127.0.0.1:8899", "NO_PROXY": "github.com"}
    assert gn.dead_proxy_finding(env, opener=refused) is None
    assert gn.dead_proxy_finding(env, opener=refused, hosts=gn._GITHUB_SUFFIXES + gn._GH_EXTRA_HOSTS) is not None
    # Operator codex-vpn workaround: every GitHub host bypassed keeps working.
    env["NO_PROXY"] = "github.com, api.github.com, *.githubusercontent.com, codeload.github.com:443"
    assert gn.dead_proxy_finding(env, opener=refused, hosts=gn._GITHUB_SUFFIXES + gn._GH_EXTRA_HOSTS) is None


def test_proxy_credentials_never_leave_the_finding():
    def refused(host, port):
        raise OSError('down')
    finding = gn.dead_proxy_finding({"HTTPS_PROXY": "http://user:s3cret@127.0.0.1:8899/x"}, opener=refused)
    assert finding is not None and 's3cret' not in finding and 'user' not in finding.replace('proxy ', '').split()[0]
    assert '127.0.0.1:8899' in finding


def test_dead_proxy_refuses_before_clone_with_fixed_reason(tmp_path, monkeypatch):
    import socket
    from agent_hub_host import dispatch_diagnostics
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8899")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8899")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    def refused(*args, **kwargs):
        raise ConnectionRefusedError(111, 'refused')
    monkeypatch.setattr(socket, 'create_connection', refused)
    label = "repo-0123abcd-DataTalksClub__demo"
    with pytest.raises(ValueError, match="^repo_proxy_unreachable: DataTalksClub/demo: proxy 127.0.0.1:8899"):
        repo_workspace.ensure_repo_workspace({"work_root": str(tmp_path)}, label, gh_bin="")
    try:
        repo_workspace.ensure_repo_workspace({"work_root": str(tmp_path)}, label, gh_bin="")
    except ValueError as exc:
        diagnostic = dispatch_diagnostics.classify(exc)
        assert diagnostic['reason'] == 'repo_proxy_unreachable'
        assert diagnostic['stage'] == 'dispatch_route'
        assert '127.0.0.1:8899' in diagnostic.get('tail', '')
        assert 's3cret' not in __import__('json').dumps(diagnostic)
    else:
        raise AssertionError('expected repo_proxy_unreachable')


def test_gh_auth_finding_names_failure_without_tokens():
    def runner(argv, cwd, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="error: token=abc-secret is invalid")
    finding = gn.gh_auth_finding("/usr/bin/gh", runner=runner)
    assert finding is not None and finding.startswith('gh auth failed')
    assert 'abc-secret' not in finding
    assert gn.gh_auth_finding("/usr/bin/gh", runner=lambda *a: SimpleNamespace(returncode=0, stdout="ok", stderr="")) is None
    assert gn.gh_auth_finding("", runner=None) is None


def test_clone_failure_tail_reaches_launch_error_without_secrets():
    from agent_hub_host import dispatch_diagnostics
    exc = ValueError('repo_clone_failed: DataTalksClub/demo: fatal: unable to access token=zzz-secret')
    diagnostic = dispatch_diagnostics.classify(exc)
    assert diagnostic['reason'] == 'repo_clone_failed'
    assert 'unable to access' in diagnostic.get('tail', '')
    assert 'zzz-secret' not in __import__('json').dumps(diagnostic)
