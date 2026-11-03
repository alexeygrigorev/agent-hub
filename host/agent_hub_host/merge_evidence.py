"""Merge evidence for the Hub merge gate (intake 2e00e01f, docs/MERGE-GATE.md).

``hub intake merge-check <intake>`` runs git (ancestry against a freshly
fetched origin/main) and ``gh`` (Deploy workflow runs for the exact commits)
locally and posts the result to Hub. Hub never runs git itself. Anything that
cannot be determined stays unknown; ancestry errors never read as merged and a
missing deploy run never reads as green.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

SHA = re.compile(r'^[0-9a-f]{7,40}$', re.I)
DEPLOY_WORKFLOW = 'deploy.yml'
GIT_TIMEOUT = 30
FETCH_TIMEOUT = 90
MAX_COMMITS = 64
SKIP_CI = re.compile(r'\[(?:skip ci|ci skip|no ci)\]', re.I)


def _run(runner: Callable, argv: list, cwd: str, timeout: int = GIT_TIMEOUT):
    try:
        extra = {}
        if runner is subprocess.run:
            from .git_noninteractive import noninteractive_env
            extra['env'] = noninteractive_env()
            extra['stdin'] = subprocess.DEVNULL
        return runner(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, **extra)
    except (OSError, subprocess.SubprocessError):
        return None


UUID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I)
DEFAULT_REMOTE = 'git@github.com:alexeygrigorev/agent-hub.git'


def shas_from_text(text: str, exclude: tuple = ()) -> list:
    """Commit-*candidate* tokens (7-40 hex with a letter and a digit). Only candidates:
    callers must verify them with ``existing_commits``. Uuids (and fragments of ids in
    ``exclude``) are intake/session ids, never commits."""
    text = UUID.sub(' ', text or '')
    skip = {frag.lower() for ident in exclude for frag in re.split(r'[-\s]+', str(ident or '')) if frag}
    found = []
    for token in re.findall(r'\b[0-9a-f]{7,40}\b', text, re.I):
        low = token.lower()
        if re.search(r'[a-f]', low) and re.search(r'\d', low) and low not in found and low not in skip:
            found.append(low)
    return found[:MAX_COMMITS]


def existing_commits(repo: str, candidates: list, runner: Callable = subprocess.run) -> list:
    """Candidates that are real commits in ``repo`` (git cat-file -e <sha>^{commit})."""
    return [c for c in candidates
            if (p := _run(runner, ['git', 'cat-file', '-e', c + '^{commit}'], repo)) is not None and p.returncode == 0]


def _supersession_state(history: list) -> tuple[set[str], set[int]]:
    """Return unambiguously superseded report ids and valid superseding indexes."""
    reports = {}
    events = history if isinstance(history, list) else []
    duplicate_ids = set()
    for index, event in enumerate(events):
        if isinstance(event, dict) and event.get('kind') == 'report' and isinstance(event.get('id'), str):
            ident = event['id'].lower()
            if ident in reports:
                duplicate_ids.add(ident)
            reports[ident] = (index, event)
    for ident in duplicate_ids:
        reports.pop(ident, None)

    supersession_claims = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get('kind') != 'report':
            continue
        event_id = event.get('id')
        if not isinstance(event_id, str) or reports.get(event_id.lower(), (None,))[0] != index:
            continue
        current_reference = event.get('reference')
        if not isinstance(current_reference, str) or not SHA.fullmatch(current_reference.strip()):
            continue
        targets = event.get('supersedes')
        if not isinstance(targets, list) or not targets or len(targets) > 32:
            continue
        normalized = [target.lower() for target in targets if isinstance(target, str)]
        if len(normalized) != len(targets) or len(set(normalized)) != len(normalized):
            continue
        valid = True
        for target in normalized:
            prior = reports.get(target)
            reference = prior[1].get('reference') if prior else None
            if (prior is None or prior[0] >= index or not isinstance(reference, str)
                    or not SHA.fullmatch(reference.strip())):
                valid = False
                break
        if valid:
            supersession_claims.append((index, normalized))

    claim_counts = {}
    for _, targets in supersession_claims:
        for target in targets:
            claim_counts[target] = claim_counts.get(target, 0) + 1
    # Ambiguous history must keep every target of a competing claim required.
    # Reject the entire claim when any target is contested; otherwise a
    # multi-target claim could still hide its uncontested targets.
    valid_claims = [
        (index, targets) for index, targets in supersession_claims
        if all(claim_counts[target] == 1 for target in targets)
    ]
    superseded = {target for _, targets in valid_claims for target in targets}
    valid_superseding_indexes = {index for index, _ in valid_claims}
    return superseded, valid_superseding_indexes


def _active_report_indexes(history: list) -> list[int]:
    """Return report indexes that contribute a required checkpoint reference."""
    events = history if isinstance(history, list) else []
    valid = {
        index for index, entry in enumerate(events)
        if isinstance(entry, dict) and entry.get('kind') == 'report'
        and isinstance(entry.get('reference'), str)
        and SHA.fullmatch(entry['reference'].strip())
    }
    explicit_indexes = [
        index for index, entry in enumerate(events)
        if isinstance(entry, dict) and entry.get('kind') == 'report' and 'supersedes' in entry
    ]
    if not explicit_indexes:
        return [max(valid)] if valid else []

    first_explicit_index = explicit_indexes[0]
    superseded, _ = _supersession_state(events)
    latest_legacy = max((index for index in valid if index < first_explicit_index), default=None)
    active = []
    if latest_legacy is not None:
        event = events[latest_legacy]
        ident = event.get('id')
        if not isinstance(ident, str) or ident.lower() not in superseded:
            active.append(latest_legacy)
    active.extend(
        index for index in sorted(valid)
        if index >= first_explicit_index
        and not (isinstance(events[index].get('id'), str)
                 and events[index]['id'].lower() in superseded)
    )
    return active


def active_report_references(history: list) -> list:
    """Return report/commit refs, excluding only explicitly superseded reports.

    Supersession points to a prior report event id, never to a SHA by itself.
    Malformed or dangling supersession data is ignored so the original report
    remains required and missing commits continue to fail closed.
    """
    events = history if isinstance(history, list) else []
    superseded, _ = _supersession_state(events)
    refs = []
    for event in events:
        if not isinstance(event, dict) or event.get('kind') not in ('report', 'commit'):
            continue
        if event.get('kind') == 'report' and isinstance(event.get('id'), str) and event['id'].lower() in superseded:
            continue
        reference = event.get('reference')
        if isinstance(reference, str) and SHA.fullmatch(reference.strip()):
            refs.append(reference.strip())
    return refs


def latest_active_report_reference(history: list) -> Optional[str]:
    """Return the SHA of the latest report checkpoint that remains active."""
    events = history if isinstance(history, list) else []
    indexes = _active_report_indexes(events)
    if not indexes:
        return None
    reference = events[indexes[-1]].get('reference')
    return reference.strip().lower() if isinstance(reference, str) else None


def explicit_commit_refs(history: list) -> list:
    """Return commit refs, using explicit links when an intake opts into them.

    Older histories keep the latest-report checkpoint behavior. When explicit
    supersession begins, the legacy prefix still contributes only its latest
    report checkpoint; later reports use event-id supersession. Commit events
    always remain cumulative.
    """
    events = history if isinstance(history, list) else []
    active_reports = set(_active_report_indexes(events))
    candidates = []
    for index, entry in enumerate(events):
        if not isinstance(entry, dict) or entry.get('kind') not in ('report', 'commit'):
            continue
        if entry.get('kind') == 'report' and index not in active_reports:
            continue
        reference = entry.get('reference')
        if isinstance(reference, str) and SHA.fullmatch(reference.strip()):
            candidates.append(reference.strip())

    refs = []
    for ref in candidates:
        normalized = ref.strip().lower()
        if SHA.fullmatch(normalized) and normalized not in refs:
            refs.append(normalized)
    return refs


def superseding_report_checkpoint(history: list, merge_state: Any) -> Optional[str]:
    """Return a safe replacement SHA for a newer, sole active report checkpoint.

    ``evidence_at`` records when merge evidence was last submitted. ``activity_at``
    also moves when the watcher defers an intake for a live session, so it cannot
    establish that a report predates the recorded evidence. A checkpoint may
    replace stored state only when it is the only active report reference.
    """
    state = merge_state if isinstance(merge_state, dict) else {}
    evidence_at = state.get('evidence_at')
    if type(evidence_at) is not int or evidence_at < 0:
        return None
    events = history if isinstance(history, list) else []
    _, superseding_indexes = _supersession_state(events)
    active = _active_report_indexes(events)
    if len(active) != 1 or active[0] not in superseding_indexes:
        return None
    event = events[active[0]]
    captured_at = event.get('captured_at') if isinstance(event, dict) else None
    reference = event.get('reference') if isinstance(event, dict) else None
    if (type(captured_at) is int and captured_at > evidence_at
            and isinstance(reference, str) and SHA.fullmatch(reference.strip())):
        return reference.strip().lower()
    return None


def superseding_report_is_newer_than_merge_state(history: list, merge_state: Any) -> bool:
    """Compatibility predicate for callers that only need a checkpoint flag."""
    return superseding_report_checkpoint(history, merge_state) is not None


def hub_recorded_shas(merge_state: Any) -> list:
    """Commit SHAs Hub already tracks for the worktree (branch head from host status plus
    earlier evidence). Without these a merged worktree whose intake carries no report/DONE
    sha yields no commits, hence no deploy evidence (intake 3f2011c2)."""
    state = merge_state if isinstance(merge_state, dict) else {}
    recorded_commits = state.get('commits')
    entries = recorded_commits if isinstance(recorded_commits, (list, tuple)) else []
    refs = [state.get('head')] + [c.get('sha') for c in entries if isinstance(c, dict)]
    out = []
    for ref in refs:
        ref = str(ref or '').strip().lower()
        if SHA.match(ref) and ref not in out:
            out.append(ref)
    return out


def own_clone(runner: Callable = subprocess.run, remote: Optional[str] = None) -> str:
    """Private checkout of the project repo (never the caller's cwd)."""
    remote = remote or os.environ.get('AGENT_HUB_MERGE_CHECK_REMOTE') or DEFAULT_REMOTE
    base = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'agent-hub' / 'merge-check-repo'
    if not (base / '.git').exists() and not (base / 'HEAD').exists():
        base.parent.mkdir(parents=True, exist_ok=True)
        proc = _run(runner, ['git', 'clone', '--quiet', '--no-checkout', remote, str(base)], str(base.parent), 300)
        if proc is None or proc.returncode != 0:
            raise RuntimeError('could not clone the project repository for merge-check; pass --repo DIR')
    return str(base)


def _is_ancestor(runner, repo, sha, ref):
    proc = _run(runner, ['git', 'merge-base', '--is-ancestor', sha, ref], repo)
    if proc is None or proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def deploy_state(repo: str, sha: str, runner: Callable = subprocess.run) -> dict:
    """Deploy outcome for ``sha`` on main: the earliest-or-any Deploy run whose
    head contains it. [skip ci] commits never deploy on their own, but a later
    Deploy run for a descendant main SHA can still prove they were deployed."""
    subject = _run(runner, ['git', 'log', '-1', '--format=%B', sha], repo)
    skip_ci = subject is not None and subject.returncode == 0 and SKIP_CI.search(subject.stdout or '')
    proc = _run(runner, ['gh', 'run', 'list', '--workflow', DEPLOY_WORKFLOW, '--branch', 'main', '--limit', '40',
                         '--json', 'databaseId,headSha,conclusion,status,url'], repo, FETCH_TIMEOUT)
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
        if isinstance(head, str) and SHA.match(head) and _is_ancestor(runner, repo, sha, head):
            containing.append(run)

    def run_evidence(state: str, run: dict) -> dict:
        head = run.get('headSha')
        run_id = run.get('databaseId')
        if isinstance(run_id, int) and not isinstance(run_id, bool):
            run_id = str(run_id)
        elif not isinstance(run_id, str):
            run_id = ''
        result = {'state': state, 'sha': sha}
        if run_id.strip():
            result['run_id'] = run_id.strip()[:32]
        if isinstance(run.get('conclusion'), str) and run['conclusion'].strip():
            result['conclusion'] = run['conclusion'].strip()[:32]
        if isinstance(head, str) and SHA.match(head):
            result['deployed_sha'] = head.lower()
        if isinstance(run.get('url'), str) and run['url'].strip():
            result['run'] = run['url'].strip()[:300]
        return result

    for run in containing:
        if run.get('conclusion') == 'success':
            return run_evidence('success', run)
    for run in containing:
        if run.get('status') in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
            return run_evidence('pending', run)
    if containing:
        return run_evidence('failure', containing[0])
    if skip_ci:
        return {'state': 'skipped', 'sha': sha}
    return {'state': 'missing', 'sha': sha}


def fetch_origin(repo: str, runner: Callable = subprocess.run) -> None:
    proc = _run(runner, ['git', 'fetch', '--quiet', 'origin', 'main'], repo, FETCH_TIMEOUT)
    if proc is None or proc.returncode != 0:
        raise RuntimeError('git fetch origin main failed; merge state cannot be verified')


def collect(repo: str, shas: Optional[list] = None, runner: Callable = subprocess.run, fetch: bool = True,
            deploy: bool = True, include_head: bool = True, preferred_deploy_sha: Optional[str] = None) -> dict:
    """Evidence body for POST /api/intake/tasks/<id>/merge-evidence."""
    out: dict[str, Any] = {}
    if fetch:
        fetch_origin(repo, runner)
    head = _run(runner, ['git', 'rev-parse', 'HEAD'], repo) if include_head else None
    head_sha = (head.stdout or '').strip() if head is not None and head.returncode == 0 else ''
    if SHA.match(head_sha):
        out['head'] = head_sha
    branch = _run(runner, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], repo) if include_head else None
    if branch is not None and branch.returncode == 0 and (branch.stdout or '').strip() not in ('', 'HEAD'):
        out['branch'] = branch.stdout.strip()[:255]
    cherry = _run(runner, ['git', 'cherry', 'origin/main', 'HEAD'], repo) if include_head else None
    if cherry is not None and cherry.returncode == 0:
        out['unmerged'] = sum(1 for line in (cherry.stdout or '').splitlines() if line.startswith('+'))
    wanted = []
    preferred = preferred_deploy_sha.lower() if SHA.match(preferred_deploy_sha or '') else ''
    for sha in ([preferred] if preferred else []) + (shas or []) + ([head_sha] if head_sha else []):
        if SHA.match(sha or '') and sha.lower() not in wanted:
            wanted.append(sha.lower())
    commits = []
    for sha in wanted[:MAX_COMMITS]:
        full = _run(runner, ['git', 'rev-parse', '--verify', '--quiet', sha + '^{commit}'], repo)
        resolved = (full.stdout or '').strip() if full is not None and full.returncode == 0 else ''
        if not SHA.match(resolved):
            commits.append({'sha': sha, 'in_main': False, 'subject': 'not found in this repository'})
            continue
        if any(c['sha'] == resolved for c in commits):
            continue
        in_main = _is_ancestor(runner, repo, resolved, 'origin/main')
        subject = _run(runner, ['git', 'log', '-1', '--format=%s', resolved], repo)
        commits.append({'sha': resolved, 'in_main': in_main is True,
                        **({'subject': subject.stdout.strip()[:160]} if subject is not None and subject.returncode == 0 else {})})
    if commits:
        out['commits'] = commits
        if not deploy:
            return out
        merged = [c for c in commits if c['in_main']]
        # The deploy that matters is the newest merged commit named in the evidence.
        if merged and len(merged) == len(commits):
            # For the private clone, the Hub-recorded worktree head is the intended
            # target; when an explicit repo is supplied, its observed HEAD takes
            # precedence. A green deploy for an older report SHA must not prove a
            # newer worktree head that only has a pending or failed deploy.
            local_head = head_sha if include_head else ''
            merged_shas = {c['sha'] for c in merged}
            target = local_head if local_head in merged_shas else preferred if preferred in merged_shas else merged[0]['sha']
            out['deploy'] = deploy_state(repo, target, runner)
        elif merged:
            out['deploy'] = {'state': 'pending', 'sha': merged[0]['sha']}
        else:
            out['deploy'] = {'state': 'missing'}
    return out
