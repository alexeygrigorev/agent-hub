#!/usr/bin/env python3
"""One-shot backfill for the merge gate (intake 2e00e01f AC7).

Scans *-wt-* worktrees under the shared git directory on THIS host, maps each to an open Hub intake
(by the `-wt-<8 hex>` suffix, the registered Hub worktree, or an intake id named
in the branch's commit subjects), collects git/deploy evidence and, with
--apply, records it with idle=true so Hub's watcher flags and requeues every
unmerged intake at its next tick. Dry run by default; prints a JSON report.
Needs operator credentials (hub CLI login). Run on each host.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from agent_hub_host import intake, merge_evidence  # noqa: E402

ID = re.compile(r'\b[0-9a-f]{8}\b')
GIT_TIMEOUT = 10
FETCH_TIMEOUT = 90


def git_process(repo, *argv, timeout=GIT_TIMEOUT):
    try:
        return subprocess.run(['git', *argv], cwd=repo, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def git(repo, *argv, timeout=GIT_TIMEOUT):
    proc = git_process(repo, *argv, timeout=timeout)
    return proc.stdout.strip() if proc is not None and proc.returncode == 0 else None


def default_root(script_path=None, home=None):
    """Find the shared git directory from this checkout, even with a managed HOME."""
    script = Path(script_path or __file__).resolve()
    checkout_parent = script.parent.parent.parent
    if checkout_parent.name == 'git':
        return checkout_parent
    return Path(home or Path.home()) / 'git'


def find_intake(path, cache):
    candidates = []
    m = re.search(r'-wt-([0-9a-f]{8})$', path.name)
    if m:
        candidates.append(m.group(1))
    log = git(path, 'log', '--format=%s%n%b', '-n', '30', 'HEAD', '--not', 'origin/main')
    candidates += ID.findall(log or '')
    lookup_error = None
    for cand in dict.fromkeys(candidates):
        if cand not in cache:
            try:
                cache[cand] = (intake.request('GET', '/api/intake/tasks/' + cand)['task'], None)
            except intake.IntakeHTTPError as exc:
                cache[cand] = (None, 'not_found' if exc.status == 404 else exc.code)
            except Exception:
                cache[cand] = (None, 'lookup_failed')
        task, error = cache[cand]
        if task:
            return task, log is not None or bool(m), None
        if error != 'not_found':
            lookup_error = error
    return None, log is not None or bool(m), lookup_error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--root', default=str(default_root()))
    args = ap.parse_args()
    report, cache, fetch_status = [], {}, {}
    for path in sorted(Path(args.root).glob('*-wt-*')):
        if not (path / '.git').exists():
            continue
        # Worktrees of one repository share refs: fetch origin/main once per repository.
        common = git(path, 'rev-parse', '--git-common-dir')
        if not common:
            report.append({'worktree': str(path), 'state': 'inspection_failed',
                           'error': 'git metadata inspection timed out or failed'})
            continue
        common = str((path / common).resolve()) if common and not os.path.isabs(common) else common
        if common and common not in fetch_status:
            fetched = git_process(path, 'fetch', '--quiet', 'origin', 'main', timeout=FETCH_TIMEOUT)
            fetch_status[common] = bool(fetched is not None and fetched.returncode == 0)
        item = {'worktree': str(path), 'branch': git(path, 'rev-parse', '--abbrev-ref', 'HEAD')}
        if common and not fetch_status.get(common, False):
            item.update(state='fetch_failed', error='origin/main could not be refreshed')
            report.append(item)
            continue
        try:
            evidence = merge_evidence.collect(str(path), [], fetch=False, deploy=False)
        except RuntimeError as exc:
            item.update(state='fetch_failed', error=str(exc)); report.append(item); continue
        if not evidence.get('head') or evidence.get('unmerged') is None:
            item.update(state='inspection_failed', error='commit ancestry could not be inspected')
            report.append(item)
            continue
        task, intake_lookup_complete, lookup_error = find_intake(path, cache)
        dirty_status = git(path, 'status', '--porcelain')
        if dirty_status is None:
            item.update(state='inspection_failed', error='git status timed out or failed')
            report.append(item)
            continue
        dirty = bool(dirty_status)
        item.update(head=evidence.get('head'), unmerged=evidence.get('unmerged'), dirty=dirty)
        if not task:
            if lookup_error:
                item.update(state='lookup_failed', error=lookup_error)
            elif not intake_lookup_complete:
                item.update(state='inspection_failed', error='intake references could not be inspected')
            else:
                item['state'] = 'no_intake' if evidence.get('unmerged') or dirty else 'merged_no_intake'
            report.append(item); continue
        item['intake'] = task['id']
        item['title'] = task['payload_data']['title']
        status = task['control']['status']
        item['intake_status'] = status
        if status in ('done', 'cancelled'):
            item['state'] = 'closed_intake' + ('_unmerged' if evidence.get('unmerged') else '')
        elif not evidence.get('unmerged') and not dirty:
            item['state'] = 'merged'
        else:
            item['state'] = 'unmerged'
            if args.apply:
                hostname = os.uname().nodename
                try:
                    intake.request('POST', '/api/intake/tasks/%s/worktrees' % task['id'], {
                        'key': 'bf-wt-' + uuid.uuid4().hex,
                        'worktree': {'machine': hostname, 'path': str(path), 'branch': item['branch']}})
                    item['recorded_worktree'] = True
                    body = {'key': 'bf-ev-' + uuid.uuid4().hex, 'idle': True,
                            'machine': hostname, 'path': str(path), **evidence}
                    body.pop('branch', None)
                    intake.request('POST', '/api/intake/tasks/%s/merge-evidence' % task['id'], body)
                    item['recorded_evidence'] = True
                except Exception as exc:
                    item.update(state='apply_failed', apply_error=intake.diagnostic(exc))
        report.append(item)
    print(json.dumps({'applied': args.apply, 'worktrees': len(report),
                      'unmerged': [r for r in report if r['state'] == 'unmerged'], 'all': report}, indent=2))


if __name__ == '__main__':
    main()
