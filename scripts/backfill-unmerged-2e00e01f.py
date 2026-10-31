#!/usr/bin/env python3
"""One-shot backfill for the merge gate (intake 2e00e01f AC7).

Scans ~/git/*-wt-* worktrees on THIS host, maps each to an open Hub intake
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


def git(repo, *argv):
    proc = subprocess.run(['git', *argv], cwd=repo, capture_output=True, text=True, timeout=60, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ''


def find_intake(path, cache):
    candidates = []
    m = re.search(r'-wt-([0-9a-f]{8})$', path.name)
    if m:
        candidates.append(m.group(1))
    log = git(path, 'log', '--format=%s%n%b', '-n', '30', 'HEAD', '--not', 'origin/main')
    candidates += ID.findall(log)
    for cand in dict.fromkeys(candidates):
        if cand not in cache:
            try:
                cache[cand] = intake.request('GET', '/api/intake/tasks/' + cand)['task']
            except Exception:
                cache[cand] = None
        if cache[cand]:
            return cache[cand]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--root', default=str(Path.home() / 'git'))
    args = ap.parse_args()
    report, cache = [], {}
    for path in sorted(Path(args.root).glob('*-wt-*')):
        if not (path / '.git').exists():
            continue
        item = {'worktree': str(path), 'branch': git(path, 'rev-parse', '--abbrev-ref', 'HEAD')}
        try:
            evidence = merge_evidence.collect(str(path), [])
        except RuntimeError as exc:
            item.update(state='fetch_failed', error=str(exc)); report.append(item); continue
        task = find_intake(path, cache)
        dirty = bool(git(path, 'status', '--porcelain'))
        item.update(head=evidence.get('head'), unmerged=evidence.get('unmerged'), dirty=dirty,
                    deploy=(evidence.get('deploy') or {}).get('state'))
        if not task:
            item['state'] = 'no_intake' if evidence.get('unmerged') or dirty else 'merged_no_intake'
            report.append(item); continue
        item['intake'] = task['id']
        item['title'] = task['payload_data']['title']
        status = task['control']['status']
        item['intake_status'] = status
        if status in ('done', 'cancelled'):
            item['state'] = 'closed_intake' + ('_unmerged' if evidence.get('unmerged') else '')
        elif not evidence.get('unmerged') and not dirty and (evidence.get('deploy') or {}).get('state') in ('success', 'skipped', 'not_required'):
            item['state'] = 'merged'
        else:
            item['state'] = 'unmerged'
            if args.apply:
                hostname = os.uname().nodename
                intake.request('POST', '/api/intake/tasks/%s/worktrees' % task['id'], {
                    'key': 'bf-wt-' + uuid.uuid4().hex, 'worktree': {'machine': hostname, 'path': str(path), 'branch': item['branch']}})
                body = {'key': 'bf-ev-' + uuid.uuid4().hex, 'idle': True, 'machine': hostname, 'path': str(path), **evidence}
                body.pop('branch', None)
                intake.request('POST', '/api/intake/tasks/%s/merge-evidence' % task['id'], body)
                item['recorded'] = True
        report.append(item)
    print(json.dumps({'applied': args.apply, 'worktrees': len(report),
                      'unmerged': [r for r in report if r['state'] == 'unmerged'], 'all': report}, indent=2))


if __name__ == '__main__':
    main()
