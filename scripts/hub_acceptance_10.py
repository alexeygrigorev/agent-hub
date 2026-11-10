#!/usr/bin/env python3
"""Phase 7 final acceptance: ten consecutive model-less Hub starts.

Fails closed: any missing evidence is a failure, never a skip. Only sessions
this run started are ever stopped. The live run needs ``--live`` and
HUB_ACCEPTANCE_READY=1 (orchestrator readiness); without them the script
exits 2 before touching Hub. All I/O goes through an injectable ``Runner`` so
the offline tests never reach the network.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid

COUNT = 10
RUNNING_BUDGET_S = 150
STOP_BUDGET_S = 60
CAPTURE_BUDGET_S = 90
MIN_MODELS = 3
MAX_LUNA = 3
HOST = 'hetzner-aplexer'
EVIDENCE_GREEN = ('success', 'green', 'passed', 'pass')


class Runner:
    """Executes `hub`/`a` commands; tests replace it with a fake."""

    def __init__(self, hub='hub', a='a'):
        self.bins = {'hub': hub, 'a': a}

    def run(self, tool, args, timeout=300):
        proc = subprocess.run([self.bins[tool], *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr

    def json(self, tool, args, timeout=300):
        rc, out, err = self.run(tool, args, timeout)
        if rc != 0:
            raise RuntimeError('%s %s failed rc=%s: %s' % (tool, ' '.join(args[:2]), rc, (err or out)[-300:]))
        return json.loads(out)


def text_of(obj):
    return json.dumps(obj, sort_keys=True).lower()


def is_astra(*parts):
    return any('astra' in str(p).lower() for p in parts)


def is_luna(*parts):
    return any('luna' in str(p).lower() for p in parts)


def check_evidence(path, commit):
    """Same-commit canary + CI e2e green, no dev alert; anything missing fails."""
    problems = []
    if not path:
        return ['evidence file missing (--evidence)']
    try:
        with open(path, encoding='utf-8') as fh:
            ev = json.load(fh)
    except (OSError, ValueError) as exc:
        return ['evidence unreadable: %s' % exc]
    if not isinstance(ev, dict):
        return ['evidence is not an object']
    if not commit:
        problems.append('deployed commit unknown (--commit)')
    for key in ('canary_commit', 'ci_e2e_commit'):
        if not ev.get(key):
            problems.append('%s missing' % key)
        elif commit and ev[key] != commit:
            problems.append('%s %s != %s' % (key, ev[key], commit))
    for key in ('canary', 'ci_e2e'):
        if str(ev.get(key, '')).lower() not in EVIDENCE_GREEN:
            problems.append('%s not green (%r)' % (key, ev.get(key)))
    alerts = ev.get('dev_alerts')
    if not isinstance(alerts, int) or isinstance(alerts, bool):
        problems.append('dev_alerts missing')
    elif alerts != 0:
        problems.append('dev alerts during run: %d' % alerts)
    return problems


def make_probe():
    a, b = random.randint(1001, 9999), random.randint(1001, 9999)
    nonce = uuid.uuid4().hex[:8]
    prompt = ('Hub acceptance probe %s. Use your shell tool to run `echo $((%d*%d))`, '
              'then reply with the printed number and stop.' % (nonce, a, b))
    return prompt, str(a * b)


def probe_output_ok(captured, prompt, expected):
    """Tool output beyond the prompt: the computed product must appear outside any echo of the prompt."""
    stripped = (captured or '').replace(prompt, '')
    return len(re.findall(r'(?<!\d)%s(?!\d)' % re.escape(expected), stripped)) >= 1


def one_start(r, i, clock, sleep, pick_json=True):
    rec = {'n': i, 'ok': False, 'errors': []}
    err = rec['errors']
    pick = {}
    if pick_json:
        try:
            pick = r.json('hub', ['sessions', 'pick', '--json'], 60)
        except Exception as exc:  # fail closed, keep going to record
            err.append('pick failed: %s' % exc)
    pool = pick.get('pool') if isinstance(pick, dict) else None
    rec['shares'] = {row.get('policy_id'): row.get('share') for row in pool or [] if isinstance(row, dict)}
    rec['pool_size'] = len(pool or [])
    prompt, expected = make_probe()
    key = 'acc10-%s' % uuid.uuid4().hex[:12]
    t0 = clock()
    try:
        # No --engine/--model/--tier overrides by construction.
        created = r.json('hub', ['sessions', 'start', '--repo', 'alexeygrigorev/agent-hub', '--task-kind', 'ask',
                                 '--prompt', prompt, '--key', key, '--wait', '--timeout', str(RUNNING_BUDGET_S)],
                         RUNNING_BUDGET_S + 60)
    except Exception as exc:
        err.append('start failed: %s' % exc)
        return rec
    rec['elapsed_s'] = round(clock() - t0, 1)
    sid = created.get('id') if isinstance(created, dict) else None
    rec['id'] = sid
    if not sid:
        err.append('start returned no id')
        return rec
    if rec['elapsed_s'] > RUNNING_BUDGET_S:
        err.append('running took %.1fs > %ds' % (rec['elapsed_s'], RUNNING_BUDGET_S))
    try:
        row = r.json('hub', ['sessions', 'show', sid, '--json'], 60)
    except Exception as exc:
        err.append('show failed: %s' % exc)
        return rec
    rec['row'] = row
    rec['model'] = row.get('model_id') or row.get('model')
    rec['engine'] = row.get('engine')
    native = row.get('native_id') or (row.get('binding') or {}).get('native_id')
    rec['native_id'] = native
    if row.get('state') != 'running':
        err.append('state %r not running' % row.get('state'))
    if not rec['model']:
        err.append('model missing on session row')
    if is_astra(rec['model'], rec['engine'], created.get('route'), created.get('policy_id')):
        err.append('astra picked')
    if is_luna(rec['model'], created.get('route'), created.get('policy_id')) and rec['pool_size'] <= 1:
        err.append('luna picked from a degraded one-model pool')
    if rec['pool_size'] == 1:
        err.append('one-model degraded pool')
    if not native:
        err.append('no native id for a capture')
    else:
        deadline = clock() + CAPTURE_BUDGET_S
        ok = False
        while True:
            rc, out, _ = r.run('a', ['capture', native], 60)
            if rc == 0 and probe_output_ok(out, prompt, expected):
                ok = True
                break
            if clock() >= deadline:
                break
            sleep(5)
        if not ok:
            err.append('capture shows no tool output beyond the prompt')
    return rec


def verify_listing(r, rec, ):
    """hub sessions list row carries host + workspace."""
    err = rec['errors']
    try:
        res = r.json('hub', ['sessions', 'list', '--json', '--limit', '500'], 120)
    except Exception as exc:
        err.append('list failed: %s' % exc)
        return
    rows = res.get('sessions', []) if isinstance(res, dict) else res
    mine = [s for s in rows if isinstance(s, dict) and s.get('id') == rec['id']]
    if not mine:
        err.append('session absent from hub sessions list')
        return
    if mine[0].get('machine') != HOST:
        err.append('host %r != %s' % (mine[0].get('machine'), HOST))
    if not mine[0].get('workspace'):
        err.append('workspace missing in list row')


def stop_one(r, rec, clock, sleep):
    sid, native, err = rec.get('id'), rec.get('native_id'), rec['errors']
    if not sid:
        return
    t0 = clock()
    try:
        r.json('hub', ['sessions', 'stop', sid, '--wait', '--timeout', str(STOP_BUDGET_S)], STOP_BUDGET_S + 30)
        state = r.json('hub', ['sessions', 'show', sid, '--json'], 60).get('state')
    except Exception as exc:
        err.append('stop failed: %s' % exc)
        return
    if state != 'stopped':
        err.append('state after stop %r != stopped' % state)
    while native:
        try:
            snap = r.json('a', ['snapshot', '--json', '--all'], 60)
        except Exception as exc:
            err.append('a snapshot failed: %s' % exc)
            break
        if native not in text_of(snap):
            break
        if clock() - t0 > STOP_BUDGET_S:
            err.append('native %s still listed after %ds' % (native, STOP_BUDGET_S))
            break
        sleep(3)
    rec['stop_s'] = round(clock() - t0, 1)
    if rec['stop_s'] > STOP_BUDGET_S:
        err.append('stop took %.1fs > %ds' % (rec['stop_s'], STOP_BUDGET_S))


def summarize(recs, evidence_problems):
    models = sorted({str(x['model']) for x in recs if x.get('model')})
    luna = sum(1 for x in recs if is_luna(x.get('model')))
    astra = sum(1 for x in recs if is_astra(x.get('model'), x.get('engine')))
    problems = list(evidence_problems)
    if len(recs) != COUNT:
        problems.append('only %d of %d starts attempted' % (len(recs), COUNT))
    if len(models) < MIN_MODELS:
        problems.append('%d distinct models < %d' % (len(models), MIN_MODELS))
    if luna > MAX_LUNA:
        problems.append('luna %d > %d' % (luna, MAX_LUNA))
    if astra:
        problems.append('astra %d != 0' % astra)
    for x in recs:
        for e in x['errors']:
            problems.append('start %d: %s' % (x['n'], e))
    return {'ok': not problems, 'models': models, 'luna': luna, 'astra': astra, 'problems': problems}


def run(r, evidence_path, commit, clock=time.monotonic, sleep=time.sleep, out=sys.stdout):
    evidence_problems = check_evidence(evidence_path, commit)
    recs = []
    try:
        for i in range(1, COUNT + 1):
            rec = one_start(r, i, clock, sleep)
            recs.append(rec)
            print('start %d/%d id=%s model=%s engine=%s elapsed=%ss shares=%s' % (
                i, COUNT, rec.get('id'), rec.get('model'), rec.get('engine'), rec.get('elapsed_s'),
                json.dumps(rec.get('shares'), sort_keys=True)), file=out, flush=True)
            if rec.get('id'):
                verify_listing(r, rec)
            if rec['errors'] and not rec.get('id'):
                break  # consecutive means a failed start ends the run
    finally:
        for rec in recs:  # only sessions this run started
            stop_one(r, rec, clock, sleep)
    for rec in recs:
        rec.pop('row', None)
    summary = summarize(recs, evidence_problems)
    summary['commit'] = commit
    summary['starts'] = recs
    print(json.dumps(summary, indent=2, sort_keys=True, default=str), file=out)
    print('ACCEPTANCE %s' % ('PASS' if summary['ok'] else 'FAIL'), file=out)
    return 0 if summary['ok'] else 1


def main(argv=None, runner=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live', action='store_true', help='really start ten sessions (also needs HUB_ACCEPTANCE_READY=1)')
    ap.add_argument('--evidence', help='JSON: canary/ci_e2e (+_commit), dev_alerts')
    ap.add_argument('--commit', help='deployed main commit the evidence must match')
    args = ap.parse_args(argv)
    if not args.live or os.environ.get('HUB_ACCEPTANCE_READY') != '1':
        print('refusing: live run needs --live and HUB_ACCEPTANCE_READY=1 (orchestrator readiness)', file=sys.stderr)
        return 2
    return run(runner or Runner(), args.evidence, args.commit)


if __name__ == '__main__':
    sys.exit(main())
