"""Merge evidence for the Hub merge gate (intake 2e00e01f, docs/MERGE-GATE.md).

``hub intake merge-check <intake>`` runs git (ancestry against a freshly
fetched origin/main) and ``gh`` (Deploy workflow runs for the exact commits)
locally and posts the result to Hub. Hub never runs git itself. Anything that
cannot be determined stays unknown; ancestry errors never read as merged and a
missing deploy run never reads as green.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Callable, Optional

SHA = re.compile(r'^[0-9a-f]{7,40}$', re.I)
DEPLOY_WORKFLOW = 'deploy.yml'
GIT_TIMEOUT = 30
FETCH_TIMEOUT = 90
MAX_COMMITS = 64
SKIP_CI = re.compile(r'\[(?:skip ci|ci skip|no ci)\]', re.I)


def _run(runner: Callable, argv: list, cwd: str, timeout: int = GIT_TIMEOUT):
    try:
        return runner(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def shas_from_text(text: str) -> list:
    """Commit-looking tokens (7-40 hex with at least one letter and one digit)."""
    found = []
    for token in re.findall(r'\b[0-9a-f]{7,40}\b', text or '', re.I):
        if re.search(r'[a-f]', token, re.I) and re.search(r'\d', token) and token.lower() not in found:
            found.append(token.lower())
    return found[:MAX_COMMITS]


def _is_ancestor(runner, repo, sha, ref):
    proc = _run(runner, ['git', 'merge-base', '--is-ancestor', sha, ref], repo)
    if proc is None or proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def deploy_state(repo: str, sha: str, runner: Callable = subprocess.run) -> dict:
    """Deploy outcome for ``sha`` on main: the earliest-or-any Deploy run whose
    head contains it. [skip ci] commits never deploy on their own."""
    subject = _run(runner, ['git', 'log', '-1', '--format=%B', sha], repo)
    if subject is not None and subject.returncode == 0 and SKIP_CI.search(subject.stdout or ''):
        return {'state': 'skipped', 'sha': sha}
    proc = _run(runner, ['gh', 'run', 'list', '--workflow', DEPLOY_WORKFLOW, '--branch', 'main', '--limit', '40',
                         '--json', 'headSha,conclusion,status,url'], repo, FETCH_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return {'state': 'unknown', 'sha': sha}
    import json
    try:
        runs = json.loads(proc.stdout or '[]')
    except ValueError:
        return {'state': 'unknown', 'sha': sha}
    containing = []
    for run in runs if isinstance(runs, list) else []:
        head = run.get('headSha') if isinstance(run, dict) else None
        if isinstance(head, str) and _is_ancestor(runner, repo, sha, head):
            containing.append(run)
    for run in containing:
        if run.get('conclusion') == 'success':
            return {'state': 'success', 'sha': sha, 'run': run.get('url')}
    for run in containing:
        if run.get('status') in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
            return {'state': 'pending', 'sha': sha, 'run': run.get('url')}
    if containing:
        return {'state': 'failure', 'sha': sha, 'run': containing[0].get('url')}
    return {'state': 'missing', 'sha': sha}


def collect(repo: str, shas: Optional[list] = None, runner: Callable = subprocess.run, fetch: bool = True) -> dict:
    """Evidence body for POST /api/intake/tasks/<id>/merge-evidence."""
    out: dict[str, Any] = {}
    if fetch:
        proc = _run(runner, ['git', 'fetch', '--quiet', 'origin', 'main'], repo, FETCH_TIMEOUT)
        if proc is None or proc.returncode != 0:
            raise RuntimeError('git fetch origin main failed; merge state cannot be verified')
    head = _run(runner, ['git', 'rev-parse', 'HEAD'], repo)
    head_sha = (head.stdout or '').strip() if head is not None and head.returncode == 0 else ''
    if SHA.match(head_sha):
        out['head'] = head_sha
    branch = _run(runner, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], repo)
    if branch is not None and branch.returncode == 0 and (branch.stdout or '').strip() not in ('', 'HEAD'):
        out['branch'] = branch.stdout.strip()[:255]
    cherry = _run(runner, ['git', 'cherry', 'origin/main', 'HEAD'], repo)
    if cherry is not None and cherry.returncode == 0:
        out['unmerged'] = sum(1 for line in (cherry.stdout or '').splitlines() if line.startswith('+'))
    wanted = []
    for sha in (shas or []) + ([head_sha] if head_sha else []):
        if SHA.match(sha or '') and sha.lower() not in wanted:
            wanted.append(sha.lower())
    commits = []
    for sha in wanted[:MAX_COMMITS]:
        full = _run(runner, ['git', 'rev-parse', '--verify', '--quiet', sha + '^{commit}'], repo)
        resolved = (full.stdout or '').strip() if full is not None and full.returncode == 0 else ''
        if not SHA.match(resolved):
            commits.append({'sha': sha, 'in_main': False, 'subject': 'not found in this repository'})
            continue
        in_main = _is_ancestor(runner, repo, resolved, 'origin/main')
        subject = _run(runner, ['git', 'log', '-1', '--format=%s', resolved], repo)
        commits.append({'sha': resolved, 'in_main': in_main is True,
                        **({'subject': subject.stdout.strip()[:160]} if subject is not None and subject.returncode == 0 else {})})
    if commits:
        out['commits'] = commits
        merged = [c for c in commits if c['in_main']]
        # The deploy that matters is the newest merged commit named in the evidence.
        if merged and len(merged) == len(commits):
            out['deploy'] = deploy_state(repo, merged[0]['sha'], runner)
        elif merged:
            out['deploy'] = {'state': 'pending', 'sha': merged[0]['sha']}
        else:
            out['deploy'] = {'state': 'missing'}
    return out
