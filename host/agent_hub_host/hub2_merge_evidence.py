"""GitHub merge evidence for the Hub2 merge gate (``hub intake merge-check``).

Hub2 validates and journals the evidence; it never calls GitHub itself. This
collector reads GitHub only through ``gh`` (the PR, exact-head checks, main and
the compare API for ancestry), so it needs no local checkout of the intake's
repository and works for any project the caller's ``gh`` can read. A failed
observation is still evidence: Hub shows its problems and keeps the gate shut.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

SHA = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$', re.I)
REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
PR_URL = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,9})\b')
BRANCH = re.compile(r'^[A-Za-z0-9._/-]{1,200}$')
GH_TIMEOUT = 90


def _gh(runner: Callable, argv: list):
    try:
        extra = {}
        if runner is subprocess.run:
            from .git_noninteractive import noninteractive_env
            extra.update(env=noninteractive_env(), stdin=subprocess.DEVNULL)
        proc = runner(['gh', *argv], capture_output=True, text=True, timeout=GH_TIMEOUT, check=False, **extra)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc is None or proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or '')
    except (TypeError, ValueError):
        return None


def _texts(value) -> Iterable[str]:
    """Every string inside a JSON value (bounded by the Hub history page size)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def cited_prs(repository: str, events: list) -> list:
    """PR URLs of ``repository`` cited in the intake journal, newest first."""
    found = []
    for event in reversed(events or []):
        for text in _texts(event.get('body') if isinstance(event, dict) else None):
            for match in PR_URL.finditer(text):
                if match.group(1).lower() == repository.lower():
                    url = f'https://github.com/{match.group(1)}/pull/{match.group(2)}'
                    if url not in found:
                        found.append(url)
    return found


def salvage_branches(events: list) -> list:
    """Branches the host pushed for this intake's sessions, newest first."""
    found = []
    for event in reversed(events or []):
        body = event.get('body') if isinstance(event, dict) else None
        salvage = ((body or {}).get('evidence') or {}).get('git_salvage') if isinstance(body, dict) else None
        branch = salvage.get('branch') if isinstance(salvage, dict) else None
        if isinstance(branch, str) and BRANCH.fullmatch(branch) and branch not in found:
            found.append(branch)
    return found


def find_pr(repository: str, events: list, runner: Callable = subprocess.run) -> Optional[str]:
    """The intake's PR: the newest cited one, else a PR from a salvaged branch."""
    cited = cited_prs(repository, events)
    if cited:
        return cited[0]
    for branch in salvage_branches(events):
        pulls = _gh(runner, ['pr', 'list', '--repo', repository, '--head', branch, '--state', 'all',
                             '--json', 'url,mergedAt', '--limit', '5'])
        if isinstance(pulls, list):
            merged = [p for p in pulls if isinstance(p, dict) and p.get('mergedAt') and isinstance(p.get('url'), str)]
            pick = merged or [p for p in pulls if isinstance(p, dict) and isinstance(p.get('url'), str)]
            if pick:
                return pick[0]['url']
    return None


def _checks(repository: str, sha: str, runner: Callable) -> dict:
    pages = _gh(runner, ['api', '--paginate', '--slurp', f'repos/{repository}/commits/{sha}/check-runs'])
    status = _gh(runner, ['api', f'repos/{repository}/commits/{sha}/status'])
    if not isinstance(pages, list) or not isinstance(status, dict):
        return {'checks_head_sha': sha, 'checks_state': 'unknown', 'checks_total': 0, 'checks_successful': 0,
                'checks_pending': 0, 'checks_failed': 0, 'observation_error': 'GitHub exact-head checks could not be read'}
    runs = [run for page in pages if isinstance(page, dict) and isinstance(page.get('check_runs'), list)
            for run in page['check_runs'] if isinstance(run, dict)]
    statuses = [item for item in (status.get('statuses') or []) if isinstance(item, dict)]
    succeeded = sum(1 for r in runs if r.get('status') == 'completed' and r.get('conclusion') == 'success')
    successful = sum(1 for r in runs if r.get('status') == 'completed' and r.get('conclusion') in {'success', 'skipped', 'neutral'})
    pending = sum(1 for r in runs if r.get('status') != 'completed')
    failed = len(runs) - successful - pending
    status_ok = sum(1 for s in statuses if s.get('state') == 'success')
    succeeded += status_ok
    successful += status_ok
    pending += sum(1 for s in statuses if s.get('state') == 'pending')
    failed += sum(1 for s in statuses if s.get('state') not in {'success', 'pending'})
    total = len(runs) + len(statuses)
    # GitHub reports combined state 'pending' when there are no legacy statuses at all.
    combined_ok = not statuses or status.get('state') == 'success'
    state = ('success' if total and succeeded and successful == total and combined_ok
             else 'pending' if pending or (statuses and status.get('state') == 'pending') else 'failure')
    return {'checks_head_sha': sha, 'checks_state': state, 'checks_total': total, 'checks_successful': successful,
            'checks_pending': pending, 'checks_failed': failed}


def collect(repository: str, pr: Optional[str], runner: Callable = subprocess.run,
            now: Callable = lambda: datetime.now(timezone.utc)) -> dict:
    checked_at = now().isoformat().replace('+00:00', 'Z')
    if not isinstance(repository, str) or not REPO.fullmatch(repository):
        return {'kind': 'merge_gate', 'repository': 'unknown/unknown', 'checked_at': checked_at,
                'observation_error': 'the intake project is not a GitHub owner/name repository'}
    evidence = {'kind': 'merge_gate', 'repository': repository, 'checked_at': checked_at, 'base_ref': '',
                'merged_at': None, 'head_sha': '', 'checks_head_sha': '', 'checks_state': 'unknown',
                'checks_total': 0, 'checks_successful': 0, 'checks_pending': 0, 'checks_failed': 0,
                'merge_commit_sha': '', 'main_head_sha': '', 'merge_commit_in_main': False}
    errors = []
    pull = None
    if not pr:
        errors.append(f'no pull request of {repository} is cited in the intake; merge the work through a PR and pass --pr <PR URL>')
    else:
        pull = _gh(runner, ['pr', 'view', pr, '--repo', repository, '--json', 'url,baseRefName,headRefOid,mergedAt,mergeCommit'])
        if not isinstance(pull, dict):
            errors.append(f'GitHub pull request {pr} of {repository} could not be read')
            pull = None
    if pull:
        evidence['pr_url'] = pull.get('url') if isinstance(pull.get('url'), str) else ''
        evidence['base_ref'] = pull.get('baseRefName') if isinstance(pull.get('baseRefName'), str) else ''
        evidence['merged_at'] = pull.get('mergedAt') if isinstance(pull.get('mergedAt'), str) and pull.get('mergedAt') else None
        evidence['head_sha'] = str(pull.get('headRefOid') or '').lower()
        merge = pull.get('mergeCommit')
        evidence['merge_commit_sha'] = str((merge or {}).get('oid') or '').lower() if isinstance(merge, dict) else ''
        if SHA.fullmatch(evidence['head_sha']):
            checks = _checks(repository, evidence['head_sha'], runner)
            if checks.get('observation_error'):
                errors.append(checks.pop('observation_error'))
            elif checks['checks_total'] == 0 and evidence['merged_at']:
                # A merged PR whose exact head has no check run or status at all, in a
                # repository without any Actions workflow: it runs no CI. Recorded as
                # such, never as green. A repo with workflows (skipped CI, merged
                # before checks registered) stays blocked.
                workflows = _gh(runner, ['api', f'repos/{repository}/actions/workflows'])
                if isinstance(workflows, dict) and workflows.get('total_count') == 0:
                    checks['checks_state'] = 'none'
                elif not isinstance(workflows, dict):
                    errors.append('GitHub Actions workflows of the repository could not be read')
            evidence.update(checks)
        else:
            errors.append('GitHub pull request head SHA is missing or invalid')
    branch = _gh(runner, ['api', f'repos/{repository}/branches/main'])
    main_sha = str(((branch or {}).get('commit') or {}).get('sha') or '').lower() if isinstance(branch, dict) else ''
    if not SHA.fullmatch(main_sha):
        errors.append(f'main of {repository} could not be read')
    else:
        evidence['main_head_sha'] = main_sha
        merge_sha = evidence['merge_commit_sha']
        if SHA.fullmatch(merge_sha):
            compare = _gh(runner, ['api', f'repos/{repository}/compare/{merge_sha}...{main_sha}'])
            if isinstance(compare, dict) and compare.get('status') in {'ahead', 'identical'}:
                evidence['merge_commit_in_main'] = True
            elif not isinstance(compare, dict):
                errors.append('ancestry of the merge commit in main could not be read')
    if errors:
        evidence['observation_error'] = '; '.join(errors)[:1000]
    return evidence
