#!/usr/bin/env python3
"""AC6 backfill for the ask ledger (intake 55c10deb): load Alexey's 10-28/10-29
asks into the ledger as source=backfill, run the checker and report which ones
it flags as unmapped, dropped after done, or mapped only late.

Sources, both verbatim:
  1. Hub intake history: every `intake` event captured 10-28..10-29 that cites a
     tNNNNu ref. The quoted «…» words are used when present, else the event text.
  2. Miss-log cases (bot-skills personal/developer-miss-log.md #12 #14 #15 #16
     #23) whose original ask is NOT in any intake (that is the drop).
Backfill never pages, reopens intakes or starts the reconciler.

Usage: PYTHONPATH=host python3 scripts/backfill-asks-55c10deb.py --intakes FILE [--apply]
  FILE = output of `hub intake list --all`. Without --apply it only prints the plan.
"""
import argparse
import datetime as dt
import json
import re
import sys

DEVELOPER = 'e96e39a2-c10d-48ea-a25b-e2c4240cf3ef'
BERLIN = dt.timezone(dt.timedelta(hours=1))
WINDOW = (dt.datetime(2026, 10, 28, tzinfo=BERLIN), dt.datetime(2026, 10, 30, tzinfo=BERLIN))
# Miss-log asks quoted verbatim in developer-miss-log.md whose intake was lost or never made,
# plus dropped-ideas-2026-10-31.md Section A asks (intake f530f981 AC5 cross-check).
MISS_LOG = [
    {'ref': 't1441u', 'case': 'miss #12 usage-over-time dashboard (intake e19ea9ec hard-deleted)',
     'ts': '2026-10-24T12:00:00+02:00', 'text': 'Где дашборд с тем как usage меняется с течением времени?'},
    {'ref': 't1605u', 'case': 'dropped-ideas-2026-10-31 A1 Dapier starts agents through Hub',
     'ts': '2026-10-27T10:00:00+01:00', 'text': 'remove agent running from Dapier; Dapier starts agents through Hub'},
    {'ref': 't1745u', 'case': 'miss #12 repeat',
     'ts': '2026-10-28T05:05:00+01:00', 'text': 'Просил несколько раз'},
    {'ref': 't1761u', 'case': 'miss #14 Hub prompt schedules (asked 10-27, no intake)',
     'ts': '2026-10-28T09:20:00+01:00', 'text': 'Я вчера просил'},
    {'ref': 't1762u', 'case': 'miss #14 queue rule',
     'ts': '2026-10-28T09:21:00+01:00', 'text': 'Если я что-то тебе пишу это обязательно должно попадать в очередь и очередь должна двигаться'},
    {'ref': 't1777u', 'case': 'miss #15 per-intake stats folded into a TBM aggregate',
     'ts': '2026-10-28T10:10:00+01:00', 'text': 'Я уже такое просил в рамках TBM но мне кажется мы это тоже дропнули'},
    {'ref': 't1975u', 'case': 'miss #23 jev dropped from the slack-moderation redesign',
     'ts': '2026-10-29T10:30:00+01:00', 'text': 'пишу пишу прошу что-то а в итоге все дропается'},
    {'ref': 't1977u', 'case': 'miss #23 jev spec kept in memory only',
     'ts': '2026-10-29T10:35:00+01:00', 'text': 'Я тут распинаюсь расписываю как что должно работать а это просто дропается'},
]
# The jev spec (miss #23) went into e5833490, which was closed done without jev on main.
EXPLICIT = {'t1802u': 'e5833490', 't1803u': 'e5833490', 't1804u': 'e5833490'}
THREE_WEEK_WINDOW = (dt.datetime(2026, 10, 10, tzinfo=BERLIN), dt.datetime(2026, 11, 4, tzinfo=BERLIN))


def ms(value):
    return int(dt.datetime.fromisoformat(value).timestamp() * 1000)


def from_intakes(items, window=WINDOW):
    lo, hi = (int(x.timestamp() * 1000) for x in window)
    found = {}
    for task in items:
        for event in task['payload_data'].get('history', []):
            at = event.get('captured_at') or 0
            if event.get('kind') != 'intake' or not lo <= at < hi:
                continue
            text = event.get('text') or ''
            ref = (event.get('source') or {}).get('reference') or ''
            refs = re.findall(r'\bt\d{3,5}u\b', ref + ' ' + text[:200])
            if not refs:
                continue
            quotes = re.findall(r'«([^»]{3,})»', text)
            body = '\n'.join(quotes) if quotes else text
            for r in dict.fromkeys(refs):
                if r not in found or at < found[r]['ts']:
                    found[r] = {'ref': r, 'ts': at, 'text': body[:20000], 'case': 'intake ' + task['id'][:8],
                                'intake_id': EXPLICIT.get(r)}
    return found


def cross_check_dropped_report(report_path, items):
    """Cross-check ~/tmp/agent-reports/dropped-ideas-2026-10-31.md against Hub intakes."""
    from pathlib import Path
    p = Path(report_path)
    if not p.is_file():
        return {'found': False, 'path': str(p)}
    content = p.read_text(encoding='utf-8')
    intakes_by_prefix = {}
    all_blobs = []
    for t in items:
        intakes_by_prefix[t['id'][:8]] = t
        pd = t.get('payload_data') or {}
        blob = ' '.join([pd.get('title') or '', pd.get('acceptance_criteria') or ''] + [
            (e.get('text') or '') for e in (pd.get('history') or [])
        ]).lower()
        all_blobs.append((t['id'][:8], t['status'], blob))
    section_b_ids = re.findall(r'^- ([0-9a-f]{8}) created ', content, flags=re.M)
    matched_b = [i for i in section_b_ids if i in intakes_by_prefix]
    dapier_matches = [tid for tid, st, blob in all_blobs if 'dapier' in blob and ('starts agents through hub' in blob or 'remove agent running from dapier' in blob)]
    return {
        'found': True,
        'path': str(p),
        'section_b_total': len(section_b_ids),
        'section_b_in_hub': len(matched_b),
        'section_a_dapier_covered_in_hub': dapier_matches,
    }


def plan(items, window=WINDOW):
    rows = from_intakes(items, window=window)
    for case in MISS_LOG:
        rows.setdefault(case['ref'], {**case, 'ts': ms(case['ts'])})
    return sorted(rows.values(), key=lambda r: int(r['ref'][1:-1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--intakes', required=True)
    parser.add_argument('--3weeks', dest='three_weeks', action='store_true', help='AC5 f530f981: backfill last 3 weeks (since 2026-10-10)')
    parser.add_argument('--dropped-report', default='/home/alexey/tmp/agent-reports/dropped-ideas-2026-10-31.md')
    parser.add_argument('--reconcile', action='store_true', help='Run /api/asks/reconcile pass after ingest')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    items = json.load(open(args.intakes, encoding='utf-8'))['items']
    win = THREE_WEEK_WINDOW if args.three_weeks else WINDOW
    rows = plan(items, window=win)
    cross = cross_check_dropped_report(args.dropped_report, items)
    print(f'{len(rows)} backfill asks ({sum(1 for r in rows if not r["case"].startswith("intake "))} from miss/dropped logs); cross-check: {json.dumps(cross)}', file=sys.stderr)
    if not args.apply:
        for r in rows:
            print(r['ref'], r['case'], (r.get('intake_id') or '-'), r['text'][:80].replace('\n', ' '))
        return
    from agent_hub_host import session_control
    body = lambda messages: {'source': 'backfill', 'agents': [{'agent_id': DEVELOPER, 'agent_name': 'Developer', 'messages': messages}]}
    size = lambda messages: len(json.dumps(body(messages), ensure_ascii=False).encode('utf-8'))
    # The CLI refuses bodies over session_control.MAX_REQUEST, so batches are cut by encoded size.
    batches, current = [], []
    for r in rows:
        m = {'msg_id': r['ref'], 'role': 'user', 'kind': 'message', 'ts': r['ts'], 'text': r['text'],
             **({'intake_id': r['intake_id']} if r.get('intake_id') else {})}
        if current and size(current + [m]) > session_control.MAX_REQUEST * 0.9:
            batches.append(current)
            current = []
        current.append(m)
    if current:
        batches.append(current)
    for messages in batches:
        out = session_control.request('POST', '/api/asks/ingest', body(messages))
        print(json.dumps({k: out.get(k) for k in ('accepted', 'duplicates', 'conflicts', 'rejected')}, ensure_ascii=False), file=sys.stderr)
    session_control.request('POST', '/api/asks/check', {})
    if args.reconcile:
        rec = session_control.request('POST', '/api/asks/reconcile', {'run_pass': True, 'include_backfill': True})
        print('reconcile:', json.dumps({k: rec.get(k) for k in ('state', 'created', 'appended', 'mapped', 'created_ids', 'appended_ids')}, ensure_ascii=False), file=sys.stderr)
    asks = session_control.request('GET', '/api/asks?limit=500')['asks']
    created = {t['id']: t['created_at'] for t in items}
    cases = {r['ref']: r['case'] for r in rows}
    report = []
    for a in sorted((a for a in asks if a['source'] == 'backfill'), key=lambda a: int(a['t_id'][1:-1])):
        lag = None
        if a.get('intake_id') and a['intake_id'] in created and a.get('ts'):
            lag = round((created[a['intake_id']] - a['ts']) / 60000)
        flagged = a['state'] in ('unmapped', 'dropped_after_done') or (lag is not None and lag > 30)
        report.append({'ref': a['t_id'], 'case': cases.get(a['t_id']), 'state': a['state'], 'intake': (a.get('intake_id') or '')[:8] or None,
                       'ac_line': a.get('ac_line'), 'intake_created_after_ask_min': lag, 'flagged': flagged,
                       'text': ' '.join(a['text'].split())[:90]})
    json.dump(report, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == '__main__':
    main()
