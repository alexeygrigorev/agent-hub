"""Close this workflow's own deploy-failure issue once a later deploy is confirmed live.

An issue is closed only when the public host release reports the deployed commit
and every failed deploy commit the issue records is contained in that commit.
Every check runs before the first write.
"""
import argparse
import json
import re
import subprocess
import sys

ISSUE_TITLE = "Hub2 auto-deploy failed: main is not live in production"
# The GITHUB_TOKEN identity that opens the issue in the deploy alert step: the
# REST API reports "github-actions[bot]" (issue #738), GraphQL "github-actions".
WORKFLOW_AUTHORS = ("github-actions[bot]", "github-actions")
FAILED_DEPLOY = re.compile(r"deploy of main ([0-9a-f]{40}) failed")
RELEASE_URL = "https://hub.dtcdev.click/cli/host-release.json"
REPO_RE = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
SHA_RE = r"[0-9a-f]{40}"
RUN_URL_RE = r"https://[A-Za-z0-9._/-]+/actions/runs/[0-9]+"
REQUEST_SECONDS = 30
MAX_RESPONSE_BYTES = 8 << 20
PAGE_SIZE = 100
# 1000 open issues; a repository issue list that long cannot be proven complete.
MAX_PAGES = 10


class CloseRefused(RuntimeError):
    pass


def detail(text):
    """First stderr line, bounded: a diagnostic like an HTTP status, never a payload dump."""
    lines = [line.strip() for line in text.decode("utf-8", "replace").splitlines() if line.strip()]
    return "".join(ch if ch.isprintable() else "?" for ch in lines[0])[:200] if lines else "no diagnostic"


def command(argv):
    try:
        child = subprocess.run(argv, capture_output=True, timeout=REQUEST_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        raise CloseRefused("command_unavailable") from None
    if len(child.stdout) > MAX_RESPONSE_BYTES:
        raise CloseRefused("response_too_large")
    return child


def api(endpoint, *options):
    child = command(["gh", "api", endpoint, *options])
    if child.returncode != 0:
        raise CloseRefused("api_error: " + detail(child.stderr))
    try:
        return json.loads(child.stdout)
    except ValueError:
        raise CloseRefused("api_error: invalid json") from None


def open_issues(repo):
    """Every open issue of the repository, complete or refused; pull requests are excluded."""
    issues = []
    for page in range(1, MAX_PAGES + 1):
        payload = api("repos/" + repo + "/issues?state=open&per_page=" + str(PAGE_SIZE) + "&page=" + str(page))
        if not isinstance(payload, list) or not all(isinstance(i, dict) for i in payload):
            raise CloseRefused("api_error: invalid issue list")
        issues.extend(i for i in payload if "pull_request" not in i)
        if len(payload) < PAGE_SIZE:
            return issues
    raise CloseRefused("issue_list_truncated")


def issue_comments(repo, number):
    payload = api("repos/" + repo + "/issues/" + str(number) + "/comments?per_page=" + str(PAGE_SIZE))
    if not isinstance(payload, list) or not all(isinstance(c, dict) for c in payload):
        raise CloseRefused("api_error: invalid comment list")
    if len(payload) >= PAGE_SIZE:
        # An uncounted remainder could hold a failed deploy that is not contained.
        raise CloseRefused("issue_" + str(number) + "_comments_truncated")
    return payload


def contains(ancestor, descendant):
    """True when `descendant` holds every commit of `ancestor`, in the runner's full checkout."""
    child = command(["git", "merge-base", "--is-ancestor", ancestor, descendant])
    if child.returncode == 0:
        return True
    if child.returncode == 1:
        return False
    raise CloseRefused("ancestry_unknown")


def live_release():
    child = command(["curl", "--silent", "--show-error", "--fail", "--max-time", "20", RELEASE_URL])
    if child.returncode != 0:
        raise CloseRefused("release_unreadable: " + detail(child.stderr))
    try:
        payload = json.loads(child.stdout)
    except ValueError:
        raise CloseRefused("release_unreadable: invalid json") from None
    version = payload.get("host_version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not re.fullmatch(SHA_RE, version):
        raise CloseRefused("release_unreadable: invalid host_version")
    return version


def by_workflow(entry):
    user = entry.get("user") if isinstance(entry, dict) else None
    return isinstance(user, dict) and user.get("login") in WORKFLOW_AUTHORS


def failed_deploys(issue, comments):
    """Failed deploy commits this workflow recorded on the issue, newest text included."""
    texts = ([issue.get("body")] if by_workflow(issue) else []) + [c.get("body") for c in comments if by_workflow(c)]
    return sorted({sha for text in texts if isinstance(text, str) for sha in FAILED_DEPLOY.findall(text)})


def plan(issues, comments_for, sha, *, contains, live):
    """(number, failed deploys) pairs to close; refuses before any write."""
    if live != sha:
        raise CloseRefused("main_not_live")
    planned = []
    for issue in issues:
        if issue.get("title") != ISSUE_TITLE:
            continue
        number = issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise CloseRefused("issue_invalid")
        comments = comments_for(number)
        failed = failed_deploys(issue, comments)
        if not failed:
            # Also the case for a same-title issue that records no failure of this workflow.
            raise CloseRefused("issue_" + str(number) + "_records_no_deploy_workflow_failure")
        if any(s != sha and not contains(s, sha) for s in failed):
            raise CloseRefused("issue_" + str(number) + "_failed_deploy_not_contained")
        planned.append((number, failed))
    return planned


def close(repo, number, failed, sha, run_url):
    body = ("Automatic production deploy of main " + sha + " succeeded and "
            + RELEASE_URL + " reports " + sha + ", which contains the failed deploy commit(s) "
            + ", ".join(failed) + ". Closing this issue automatically.\n\nDeploy run: " + run_url)
    api("-X", "PATCH", "repos/" + repo + "/issues/" + str(number), "-f", "state=closed", "-f", "state_reason=completed")
    api("-X", "POST", "repos/" + repo + "/issues/" + str(number) + "/comments", "-f", "body=" + body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    if not (re.fullmatch(REPO_RE, args.repo) and re.fullmatch(SHA_RE, args.sha)
            and re.fullmatch(RUN_URL_RE, args.run_url)):
        print("refusing to close a deploy-failure issue: selection_invalid", file=sys.stderr)
        return 1
    try:
        planned = plan(open_issues(args.repo), lambda number: issue_comments(args.repo, number), args.sha,
                       contains=contains, live=live_release())
        for number, failed in planned:
            close(args.repo, number, failed, args.sha, args.run_url)
            print("Closed issue #" + str(number) + ": main " + args.sha + " is live and contains "
                  + ", ".join(failed))
    except CloseRefused as error:
        print("refusing to close a deploy-failure issue: " + str(error), file=sys.stderr)
        return 1
    if not planned:
        print("No open deploy-failure issue; main " + args.sha + " is live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
