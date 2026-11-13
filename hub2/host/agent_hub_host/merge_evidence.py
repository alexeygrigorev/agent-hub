"""Collect GitHub PR, exact-head check and origin/main evidence for Hub2.

Hub never shells out to git or gh. This command runs locally and posts even a
failed observation so the Hub can keep the task open and explain the gap.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

SHA = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$', re.I)
REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')


def _run(runner: Callable, argv: list[str], cwd: str, timeout: int = 30):
    try:
        extra = {}
        if runner is subprocess.run:
            from .git_noninteractive import noninteractive_env
            extra.update(env=noninteractive_env(), stdin=subprocess.DEVNULL)
        return runner(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                      check=False, **extra)
    except (OSError, subprocess.SubprocessError):
        return None


def _repo_from_remote(remote: str) -> Optional[str]:
    value = (remote or '').strip()
    match = re.fullmatch(r'(?:git@|ssh://git@)github\.com[:/]([^/]+/[^/]+?)(?:\.git)?', value)
    if match:
        candidate = match.group(1)
    else:
        parsed = urlparse(value)
        if parsed.hostname not in {'github.com', 'www.github.com'}:
            return None
        candidate = parsed.path.strip('/').removesuffix('.git')
    return candidate if REPO.fullmatch(candidate) else None


def _json(proc):
    if proc is None or proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or '')
    except (TypeError, ValueError):
        return None


def _checks(repo: str, sha: str, cwd: str, runner: Callable) -> dict:
    runs_proc = _run(runner, ['gh', 'api', '--paginate', '--slurp',
                              f'repos/{repo}/commits/{sha}/check-runs'], cwd, 90)
    status_proc = _run(runner, ['gh', 'api', f'repos/{repo}/commits/{sha}/status'], cwd, 90)
    runs_page = _json(runs_proc)
    status_doc = _json(status_proc)
    if not isinstance(runs_page, list) or not isinstance(status_doc, dict):
        return {'checks_head_sha': sha, 'checks_state': 'unknown', 'checks_total': 0,
                'checks_successful': 0, 'checks_pending': 0, 'checks_failed': 0,
                'observation_error': 'GitHub exact-head checks could not be read'}
    runs = []
    for page in runs_page:
        if isinstance(page, dict) and isinstance(page.get('check_runs'), list):
            runs.extend(item for item in page['check_runs'] if isinstance(item, dict))
    statuses = status_doc.get('statuses')
    statuses = statuses if isinstance(statuses, list) else []
    combined_state = status_doc.get('state')
    total = len(runs) + len(statuses)
    successful = sum(1 for run in runs if run.get('status') == 'completed' and run.get('conclusion') == 'success')
    pending = sum(1 for run in runs if run.get('status') != 'completed')
    failed = len(runs) - successful - pending
    successful += sum(1 for item in statuses if isinstance(item, dict) and item.get('state') == 'success')
    pending += sum(1 for item in statuses if isinstance(item, dict) and item.get('state') == 'pending')
    failed += sum(1 for item in statuses if isinstance(item, dict) and item.get('state') not in {'success', 'pending'})
    state = 'success' if total > 0 and successful == total and combined_state == 'success' else \
        'pending' if pending or combined_state == 'pending' else 'failure'
    return {'checks_head_sha': sha, 'checks_state': state, 'checks_total': total,
            'checks_successful': successful, 'checks_pending': pending, 'checks_failed': failed}


def collect(repo_path: Optional[str] = None, pr: Optional[str] = None,
            runner: Callable = subprocess.run, now: Callable = lambda: datetime.now(timezone.utc)) -> dict:
    cwd = os.path.abspath(repo_path or os.getcwd())
    checked_at = now().isoformat().replace('+00:00', 'Z')
    root_proc = _run(runner, ['git', 'rev-parse', '--show-toplevel'], cwd)
    if root_proc is None or root_proc.returncode != 0:
        return {'kind': 'merge_gate', 'repository': 'unknown/unknown', 'checked_at': checked_at,
                'observation_error': 'current directory is not a git repository'}
    cwd = (root_proc.stdout or '').strip()
    remote_proc = _run(runner, ['git', 'remote', 'get-url', 'origin'], cwd)
    repository = _repo_from_remote((remote_proc.stdout or '') if remote_proc and remote_proc.returncode == 0 else '')
    if not repository:
        return {'kind': 'merge_gate', 'repository': 'unknown/unknown', 'checked_at': checked_at,
                'observation_error': 'origin is not a GitHub repository'}
    evidence = {'kind': 'merge_gate', 'repository': repository, 'checked_at': checked_at,
                'base_ref': '', 'merged_at': None, 'head_sha': '', 'checks_head_sha': '',
                'checks_state': 'unknown', 'checks_total': 0, 'checks_successful': 0,
                'checks_pending': 0, 'checks_failed': 0, 'merge_commit_sha': '',
                'main_head_sha': '', 'merge_commit_in_main': False}
    command = ['gh', 'pr', 'view']
    if pr:
        command.append(pr)
    command.extend(['--repo', repository, '--json', 'url,state,baseRefName,headRefOid,mergedAt,mergeCommit'])
    pull = _json(_run(runner, command, cwd, 90))
    if not isinstance(pull, dict):
        evidence['observation_error'] = 'GitHub pull request could not be read; specify --pr if the current branch has no PR'
    else:
        evidence['pr_url'] = pull.get('url') if isinstance(pull.get('url'), str) else ''
        evidence['base_ref'] = pull.get('baseRefName') if isinstance(pull.get('baseRefName'), str) else ''
        evidence['merged_at'] = pull.get('mergedAt') if isinstance(pull.get('mergedAt'), str) else None
        evidence['head_sha'] = str(pull.get('headRefOid') or '').lower()
        merge_commit = pull.get('mergeCommit')
        merge_sha = merge_commit.get('oid') if isinstance(merge_commit, dict) else None
        evidence['merge_commit_sha'] = str(merge_sha or '').lower()
        if SHA.fullmatch(evidence['head_sha']):
            evidence.update(_checks(repository, evidence['head_sha'], cwd, runner))
        else:
            evidence['observation_error'] = 'GitHub pull request head SHA is missing or invalid'
    fetch = _run(runner, ['git', 'fetch', '--quiet', 'origin', 'main'], cwd, 120)
    main = _run(runner, ['git', 'rev-parse', '--verify', 'refs/remotes/origin/main^{commit}'], cwd)
    if fetch is None or fetch.returncode != 0 or main is None or main.returncode != 0:
        evidence['observation_error'] = 'origin/main could not be fetched and verified'
    else:
        evidence['main_head_sha'] = (main.stdout or '').strip().lower()
        if SHA.fullmatch(str(evidence.get('merge_commit_sha') or '')):
            ancestor = _run(runner, ['git', 'merge-base', '--is-ancestor', evidence['merge_commit_sha'], 'refs/remotes/origin/main'], cwd)
            evidence['merge_commit_in_main'] = ancestor is not None and ancestor.returncode == 0
    return evidence
