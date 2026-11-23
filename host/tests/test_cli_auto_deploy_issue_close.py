"""Auto-close of the deploy-failure issue, using synthetic issue metadata only.

No network, Git hosting or deploy: `gh` and `curl` are faked, except for one
throwaway Git repository that proves the containment check against real history."""
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest

MODULE = Path(__file__).resolve().parents[2] / "scripts/close-auto-deploy-failure-issue.py"
spec = importlib.util.spec_from_file_location("auto_deploy_issue_close_under_test", MODULE)
close = importlib.util.module_from_spec(spec)
spec.loader.exec_module(close)

REPO = "fixture/repository"
SHA = "c" * 40
FIRST, SECOND = "a" * 40, "b" * 40
URL = "https://github.com/fixture/repository/actions/runs/45247689745"
TITLE = "Hub2 auto-deploy failed: main is not live in production"


def alert(sha):
    return ("Automatic production deploy of main " + sha + " failed after green CI, so the merged fix is "
            "NOT deployed to hub.dtcdev.click or hosts.\n\nDeploy owner: fixture (repository owner). Run: " + URL)


def issue(number, body, author="github-actions[bot]", title=TITLE):
    return {"number": number, "title": title, "body": body, "user": {"login": author}}


def comment(body, author="github-actions[bot]"):
    return {"body": body, "user": {"login": author}}


def never(*args):
    raise AssertionError("no lookup or write expected")


def plan(issues, comments=(), live=SHA, contained=True):
    """Run the plan with recorded containment lookups."""
    seen = []

    def contains(ancestor, descendant):
        seen.append((ancestor, descendant))
        return contained

    planned = close.plan(issues, lambda number: list(comments), SHA, contains=contains, live=live)
    return planned, seen


def fake_run(responses):
    """`subprocess.run` stand-in that answers (exit code, stdout, stderr) in order."""
    calls = []
    replies = iter(responses)

    def run(argv, **options):
        calls.append((argv, options))
        code, out, err = next(replies)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    return calls, run


def main_with(issues, comments=(), live=SHA, contained=True):
    """Run main() against fakes; returns (exit code, recorded close calls)."""
    with mock.patch.object(close, "open_issues", return_value=issues), \
            mock.patch.object(close, "issue_comments", return_value=list(comments)), \
            mock.patch.object(close, "live_release", return_value=live), \
            mock.patch.object(close, "contains", return_value=contained), \
            mock.patch.object(close, "close") as write, \
            mock.patch("sys.argv", ["close", "--repo", REPO, "--sha", SHA, "--run-url", URL]):
        code = close.main()
    return code, write


def test_live_deploy_containing_every_recorded_failure_is_closed():
    planned, seen = plan([issue(738, alert(FIRST))], [comment(alert(SECOND))])
    assert planned == [(738, [FIRST, SECOND])]
    # Each recorded failure is tested against the deployed commit, nothing else.
    assert seen == [(FIRST, SHA), (SECOND, SHA)]


def test_rerun_of_the_failed_commit_itself_needs_no_ancestry_lookup():
    assert plan([issue(738, alert(SHA))]) == ([(738, [SHA])], [])


def test_public_release_must_report_the_deployed_commit():
    with pytest.raises(close.CloseRefused, match="^main_not_live$"):
        plan([issue(738, alert(FIRST))], live=SECOND)


def test_failed_deploy_outside_the_deployed_commit_refuses():
    with pytest.raises(close.CloseRefused, match="^issue_738_failed_deploy_not_contained$"):
        plan([issue(738, alert(FIRST))], contained=False)


def test_issue_without_a_recorded_failure_of_this_workflow_is_left_open():
    with pytest.raises(close.CloseRefused, match="^issue_738_records_no_deploy_workflow_failure$"):
        plan([issue(738, "looks broken")])


def test_someone_elses_same_title_issue_and_quote_are_not_ours():
    foreign = [issue(738, alert(FIRST), author="someone")]
    with pytest.raises(close.CloseRefused, match="^issue_738_records_no_deploy_workflow_failure$"):
        plan(foreign, [comment(alert(SECOND), author="someone")])


def test_the_login_github_reports_for_the_workflow_token_is_recognized():
    # Issue #738 as the REST API returns it: the author is "github-actions[bot]".
    # A literal here on purpose: the fixtures above follow the script's constant.
    real = [{"number": 738, "title": TITLE, "body": alert(FIRST), "user": {"login": "github-actions[bot]", "type": "Bot"}}]
    planned, _ = plan(real, [{"body": alert(SECOND), "user": {"login": "github-actions[bot]"}}])
    assert planned == [(738, [FIRST, SECOND])]
    with pytest.raises(close.CloseRefused, match="^issue_738_records_no_deploy_workflow_failure$"):
        plan([dict(real[0], user={"login": "github-actions-evil[bot]"})])
    with pytest.raises(close.CloseRefused, match="^issue_738_records_no_deploy_workflow_failure$"):
        plan([dict(real[0], user={"login": "github-actions-evil[bot]"})],
             [{"body": alert(SECOND), "user": {"login": "someone-else[bot]"}}, None])


def test_issues_with_another_title_are_untouched():
    assert plan([issue(1, alert(FIRST), title="Other")]) == ([], [])
    assert plan([]) == ([], [])


def test_main_closes_a_resolved_failure():
    code, write = main_with([issue(738, alert(FIRST))], [comment(alert(SECOND))])
    assert code == 0
    write.assert_called_once_with(REPO, 738, [FIRST, SECOND], SHA, URL)


def test_main_verifies_every_candidate_before_the_first_write(capsys):
    issues = [issue(738, alert(FIRST)), issue(739, "no failure recorded")]
    code, write = main_with(issues)
    assert code == 1
    write.assert_not_called()
    assert "refusing to close a deploy-failure issue: issue_739_records_no_deploy_workflow_failure" in capsys.readouterr().err


def test_main_without_an_open_failure_issue_reports_live(capsys):
    with mock.patch.object(close, "open_issues", return_value=[]), \
            mock.patch.object(close, "issue_comments", side_effect=never), \
            mock.patch.object(close, "live_release", return_value=SHA), \
            mock.patch.object(close, "close", side_effect=never), \
            mock.patch("sys.argv", ["close", "--repo", REPO, "--sha", SHA, "--run-url", URL]):
        code = close.main()
    assert code == 0
    assert "No open deploy-failure issue; main " + SHA + " is live" in capsys.readouterr().out


@pytest.mark.parametrize("selection", [
    ["--repo=--upload=x/y", "--sha", SHA, "--run-url", URL],
    ["--repo", REPO, "--sha", "abc", "--run-url", URL],
    ["--repo", REPO, "--sha", SHA, "--run-url", "https://evil.example/x"],
    ["--repo", REPO, "--sha", SHA, "--run-url", URL + "/../y"],
])
def test_invalid_selection_refuses_before_any_command(selection, capsys):
    with mock.patch.object(close, "open_issues", side_effect=never), \
            mock.patch.object(close, "live_release", side_effect=never), \
            mock.patch("sys.argv", ["close", *selection]):
        code = close.main()
    assert code == 1
    assert "refusing to close a deploy-failure issue: selection_invalid" in capsys.readouterr().err


def test_open_issue_list_reads_every_page_and_skips_pull_requests():
    others = [issue(1, "x", title="Other")] * 99
    full = json.dumps(others + [{"number": 2, "pull_request": {"url": "u"}}])
    calls, run = fake_run([(0, full.encode(), b""), (0, json.dumps([issue(738, alert(FIRST))]).encode(), b"")])
    with mock.patch.object(close.subprocess, "run", run):
        issues = close.open_issues(REPO)
    assert [i["number"] for i in issues] == [1] * 99 + [738]
    assert [c[0] for c in calls] == [
        ["gh", "api", "repos/" + REPO + "/issues?state=open&per_page=100&page=1"],
        ["gh", "api", "repos/" + REPO + "/issues?state=open&per_page=100&page=2"]]
    assert all(c[1] == {"capture_output": True, "timeout": close.REQUEST_SECONDS, "check": False} for c in calls)


def test_issue_list_beyond_the_page_cap_refuses():
    full = json.dumps([issue(1, "x", title="Other")] * 100)
    calls, run = fake_run([(0, full.encode(), b"")] * (close.MAX_PAGES + 1))
    with mock.patch.object(close.subprocess, "run", run), \
            pytest.raises(close.CloseRefused, match="^issue_list_truncated$"):
        close.open_issues(REPO)
    assert len(calls) == close.MAX_PAGES


def test_truncated_comment_list_refuses():
    calls, run = fake_run([(0, json.dumps([comment("x")] * 100).encode(), b"")])
    with mock.patch.object(close.subprocess, "run", run), \
            pytest.raises(close.CloseRefused, match="^issue_738_comments_truncated$"):
        close.issue_comments(REPO, 738)
    assert calls[0][0] == ["gh", "api", "repos/" + REPO + "/issues/738/comments?per_page=100"]


@pytest.mark.parametrize("code,out,err,expected", [
    (1, b"", b"HTTP 403: Resource not accessible by integration\nsecond line\n",
     "api_error: HTTP 403: Resource not accessible by integration"),
    (1, b"", b"", "api_error: no diagnostic"),
    (0, b"not json", b"", "api_error: invalid json"),
    (0, b"[1]", b"", "api_error: invalid issue list"),
    (0, b"x" * (close.MAX_RESPONSE_BYTES + 1), b"", "response_too_large"),
])
def test_api_failures_are_fixed_refusals_with_a_bounded_diagnostic(code, out, err, expected):
    calls, run = fake_run([(code, out, err)])
    with mock.patch.object(close.subprocess, "run", run), \
            pytest.raises(close.CloseRefused, match="^" + expected + "$"):
        close.open_issues(REPO)


@pytest.mark.parametrize("error", [OSError("gone"), subprocess.TimeoutExpired(["gh"], 30)])
def test_missing_or_hung_command_is_a_fixed_refusal(error):
    with mock.patch.object(close.subprocess, "run", side_effect=error), \
            pytest.raises(close.CloseRefused, match="^command_unavailable$"):
        close.open_issues(REPO)


def test_close_closes_before_it_comments():
    calls, run = fake_run([(0, b"{}", b""), (0, b"{}", b"")])
    with mock.patch.object(close.subprocess, "run", run):
        close.close(REPO, 738, [FIRST, SECOND], SHA, URL)
    assert calls[0][0] == ["gh", "api", "-X", "PATCH", "repos/" + REPO + "/issues/738",
                           "-f", "state=closed", "-f", "state_reason=completed"]
    assert calls[1][0][:5] == ["gh", "api", "-X", "POST", "repos/" + REPO + "/issues/738/comments"]
    text = calls[1][0][6][len("body="):]
    assert "main " + SHA + " succeeded" in text and FIRST in text and SECOND in text and URL in text


def test_live_release_is_read_from_the_public_manifest():
    calls, run = fake_run([(0, json.dumps({"host_version": SHA}).encode(), b"")])
    with mock.patch.object(close.subprocess, "run", run):
        assert close.live_release() == SHA
    argv = calls[0][0]
    assert argv[0] == "curl" and argv[-1] == close.RELEASE_URL and "--fail" in argv and "--max-time" in argv


@pytest.mark.parametrize("code,out,err,expected", [
    (22, b"", b"curl: (22) The requested URL returned error: 503\n",
     "release_unreadable: curl: (22) The requested URL returned error: 503"),
    (0, b"[]", b"", "release_unreadable: invalid host_version"),
    (0, b"nope", b"", "release_unreadable: invalid json"),
])
def test_unreadable_public_release_refuses(code, out, err, expected):
    calls, run = fake_run([(code, out, err)])
    with mock.patch.object(close.subprocess, "run", run), \
            pytest.raises(close.CloseRefused, match="^" + re.escape(expected) + "$"):
        close.live_release()


def test_contains_reads_the_checkout_history(tmp_path, monkeypatch):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)

    def commit(name):
        (tmp_path / name).write_text(name)
        subprocess.run(git + ["add", name], check=True)
        subprocess.run(git + ["commit", "-q", "-m", name], check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    first = commit("first")
    second = commit("second")
    subprocess.run(["git", "checkout", "-q", "--orphan", "elsewhere"], check=True)
    other = commit("other")
    assert close.contains(first, second) is True
    assert close.contains(second, second) is True
    assert close.contains(other, second) is False
    with pytest.raises(close.CloseRefused, match="^ancestry_unknown$"):
        close.contains("f" * 40, second)
