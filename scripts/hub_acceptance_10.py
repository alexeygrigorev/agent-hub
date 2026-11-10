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
import math
import re
import shlex
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
GRACE_S = 10  # subprocess slack beyond the CLI's own --timeout
EVIDENCE_GREEN = ('success', 'green', 'passed', 'pass')


class Runner:
    """Executes `hub`/`a`/alert-sample commands; tests use fake binaries on PATH."""

    def __init__(self, hub='hub', a='a', alerts_cmd=None):
        self.bins = {'hub': [hub], 'a': [a], 'alerts': shlex.split(alerts_cmd) if alerts_cmd else None}

    def run(self, tool, args, timeout=300):
        base = self.bins.get(tool)
        if not base:
            return 127, '', 'tool %s not configured' % tool
        try:
            proc = subprocess.run([*base, *(args if tool != 'alerts' else [])], capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, '', 'timeout after %ss' % timeout
        except OSError as exc:
            return 127, '', str(exc)
        return proc.returncode, proc.stdout, proc.stderr

    def json(self, tool, args, timeout=300):
        rc, out, err = self.run(tool, args, timeout)
        if rc != 0:
            raise RuntimeError('%s %s failed rc=%s: %s' % (tool, ' '.join(args[:2]), rc, (err or out)[-300:]))
        try:
            return json.loads(out)
        except ValueError as exc:
            raise RuntimeError('%s %s returned non-JSON: %s' % (tool, ' '.join(args[:2]), exc))


def left(clock, deadline, cap=300):
    """Subprocess timeout that never runs past the deadline (min 1s so a command can still fail fast)."""
    return max(1, min(cap, deadline - clock()))


def text_of(obj):
    return json.dumps(obj, sort_keys=True).lower()


def is_astra(*parts):
    return any('astra' in str(p).lower() for p in parts)


def is_luna(*parts):
    return any('luna' in str(p).lower() for p in parts)


def check_evidence(path, commit):
    """Same-commit canary + CI e2e green. Alerts are sampled live around the run, never taken from here."""
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
    return problems


def sample_alerts(r, clock):
    """Snapshot of dev@ alert ids from the operator-supplied --alerts-cmd. Fail closed: error or bad shape raises."""
    data = r.json('alerts', [], 60)
    rows = data.get('alerts') if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError('alert sample has no alerts list')
    ids = set()
    for row in rows:
        ident = row.get('id') if isinstance(row, dict) else row
        if not isinstance(ident, (str, int)) or ident == '':
            raise RuntimeError('alert sample row without id')
        ids.add(str(ident))
    return ids


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


def session_of(view):
    """`hub sessions show --json` is {"session": {...}}; a flat or foreign shape is an error, never guessed."""
    sess = view.get('session') if isinstance(view, dict) else None
    if not isinstance(sess, dict) or not sess.get('id'):
        raise RuntimeError('show response is not {"session": {...}}')
    return sess


def list_rows(res):
    rows = res.get('sessions') if isinstance(res, dict) else res
    if rows is None and isinstance(res, dict):
        rows = res.get('items')
    if not isinstance(rows, list):
        raise RuntimeError('list response has no sessions array')
    return [x for x in rows if isinstance(x, dict)]


def row_host(row):
    return row.get('host') or row.get('machine_id') or row.get('machine')


def row_workspace(row):
    ws = row.get('workspace') or row.get('workspace_path')
    if ws:
        return ws
    ev = row.get('evidence')
    ident = ((ev or {}).get('evidence') or {}).get('native_identity') if isinstance(ev, dict) else None
    return (ident or {}).get('workspace') if isinstance(ident, dict) else None


def live_natives(r, timeout):
    """Native sessions live right now: `a list --json --running`, filtered to state running (exited rows ignored)."""
    rows = r.json('a', ['list', '--json', '--running'], timeout)
    if not isinstance(rows, list):
        raise RuntimeError('a list --json is not an array')
    return {x['id'] for x in rows if isinstance(x, dict) and x.get('id') and x.get('state', 'running') == 'running'}


def pool_problems(pool):
    """Valid, nonempty pool with finite positive shares summing to ~1."""
    if not isinstance(pool, list) or not pool:
        return ['picker pool missing or empty']
    shares = []
    for row in pool:
        sh = row.get('share') if isinstance(row, dict) else None
        if isinstance(sh, bool) or not isinstance(sh, (int, float)) or not math.isfinite(sh) or sh <= 0:
            return ['picker pool has an invalid or zero share']
        shares.append(sh)
    if abs(sum(shares) - 1) > 0.02:
        return ['picker shares sum to %.3f, not 1' % sum(shares)]
    return []


def take_pick(r, err, label):
    try:
        pick = r.json('hub', ['sessions', 'pick', '--json'], 60)
        pool = pick.get('pool') if isinstance(pick, dict) else None
    except Exception as exc:
        err.append('%s pick failed: %s' % (label, exc))
        return None
    for p in pool_problems(pool):
        err.append('%s %s' % (label, p))
    return pool if isinstance(pool, list) else None


def extract_start_pool(created):
    """Picker data the start response itself carries, if the Hub echoes any."""
    for key in ('pool', 'picker'):
        val = created.get(key)
        if isinstance(val, dict):
            val = val.get('pool')
        if isinstance(val, list):
            return val
    return None


def reconcile_by_key(r, argv, key, error_text, deadline, clock):
    """A failed `start --wait` may still have created a session. Recover its id from the error text or by replaying
    the same start with the same idempotency key (returns the existing session, never a second one)."""
    m = re.search(r'session ([0-9a-fA-F-]{8,64}) reached', error_text or '')
    if m:
        return m.group(1)
    replay = [x for x in argv if x not in ('--wait',)]
    if '--timeout' in replay:
        i = replay.index('--timeout')
        del replay[i:i + 2]
    try:
        created = r.json('hub', replay, left(clock, deadline, 60))
    except Exception:
        return None
    return created.get('id') if isinstance(created, dict) else None


def one_start(r, rec, clock, sleep, intake=None):
    err = rec['errors']
    pre = take_pick(r, err, 'pre-start')
    rec['pool_size'] = len(pre or [])
    rec['shares'] = {row.get('policy_id'): row.get('share') for row in pre or [] if isinstance(row, dict)}
    prompt, expected = make_probe()
    key = 'acc10-%s' % uuid.uuid4().hex[:12]
    argv = ['sessions', 'start', '--repo', 'alexeygrigorev/agent-hub', '--task-kind', 'ask',
            '--prompt', prompt, '--key', key, '--wait', '--timeout', str(RUNNING_BUDGET_S)]
    if intake:
        argv += ['--intake-task-id', intake]
    # No --engine/--model/--tier overrides by construction.
    t0 = clock()
    deadline = t0 + RUNNING_BUDGET_S + GRACE_S
    rec['key'] = key
    created = None
    try:
        created = r.json('hub', argv, left(clock, deadline))
    except Exception as exc:
        err.append('start failed: %s' % exc)
        sid = reconcile_by_key(r, argv, key, str(exc), t0 + RUNNING_BUDGET_S + GRACE_S + 60, clock)
        if sid:
            rec['id'] = sid  # registered for cleanup immediately
            err.append('start failed after creating session %s (stopped in cleanup)' % sid)
        return
    rec['elapsed_s'] = round(clock() - t0, 1)
    sid = created.get('id') if isinstance(created, dict) else None
    rec['id'] = sid
    if not sid:
        err.append('start returned no id')
        return
    post = take_pick(r, err, 'post-start')
    own = extract_start_pool(created)
    rec['start_model'] = created.get('model')
    if rec['elapsed_s'] > RUNNING_BUDGET_S:
        err.append('running took %.1fs > %ds' % (rec['elapsed_s'], RUNNING_BUDGET_S))
    try:
        row = session_of(r.json('hub', ['sessions', 'show', sid, '--json'], left(clock, deadline, 60)))
    except Exception as exc:
        err.append('show failed: %s' % exc)
        return
    rec['model'] = row.get('model')
    rec['engine'] = row.get('engine')
    rec['host'] = row_host(row)
    native = row.get('native_id')
    rec['native_id'] = native
    if row.get('state') != 'running':
        err.append('state %r not running' % row.get('state'))
    if not rec['model']:
        err.append('model missing on session row')
    if is_astra(rec['model'], rec['engine'], created.get('route'), created.get('policy_id')):
        err.append('astra picked')
    pools = [p for p in (own, pre, post) if p]
    degraded = rec['pool_size'] <= 1 or len(post or []) == 1 or (own is not None and len(own) <= 1)
    if own is not None:
        rec['shares'] = {x.get('policy_id'): x.get('share') for x in own if isinstance(x, dict)}
    rec['shares_source'] = 'start' if own is not None else 'pick-before-start'
    if degraded:
        err.append('one-model degraded pool')
        if is_luna(rec['model'], created.get('route'), created.get('policy_id')):
            err.append('luna picked from a degraded one-model pool')
    if rec['model'] and pools and not any(str(x.get('model')) == str(rec['model']) for p in pools for x in p
                                          if isinstance(x, dict)):
        err.append('final model %s not in the picker pool shares' % rec['model'])
    if not native:
        err.append('no native id for a capture')
        return
    try:
        if native not in live_natives(r, left(clock, deadline, 60)):
            err.append('native %s not live in a list --running' % native)
    except Exception as exc:
        err.append('a list failed: %s' % exc)
    cdeadline = clock() + CAPTURE_BUDGET_S
    ok = False
    while True:
        rc, out, _ = r.run('a', ['capture', native], left(clock, cdeadline, 60))
        if rc == 0 and probe_output_ok(out, prompt, expected):
            ok = True
            break
        if clock() >= cdeadline:
            break
        sleep(5)
    if not ok:
        err.append('capture shows no tool output beyond the prompt')


def verify_listing(r, rec, clock):
    """hub sessions list row carries host + workspace (from the actual row)."""
    err = rec['errors']
    try:
        rows = list_rows(r.json('hub', ['sessions', 'list', '--json', '--limit', '500'], 120))
    except Exception as exc:
        err.append('list failed: %s' % exc)
        return
    mine = [s for s in rows if s.get('id') == rec['id']]
    if not mine:
        err.append('session absent from hub sessions list')
        return
    host = row_host(mine[0])
    if host != HOST:
        err.append('host %r != %s' % (host, HOST))
    ws = row_workspace(mine[0])
    if not ws or not str(ws).startswith('/'):
        err.append('workspace path missing in list row (%r)' % (ws,))
    rec['listed_workspace'] = ws


def stop_one(r, rec, clock, sleep):
    """Stop within STOP_BUDGET_S total, subprocess timeouts included; native proven gone from the live list."""
    sid, native, err = rec.get('id'), rec.get('native_id'), rec['errors']
    if not sid:
        return
    t0 = clock()
    deadline = t0 + STOP_BUDGET_S
    try:
        r.json('hub', ['sessions', 'stop', sid, '--wait', '--timeout', str(STOP_BUDGET_S - 5)],
               left(clock, deadline))
        sess = session_of(r.json('hub', ['sessions', 'show', sid, '--json'], left(clock, deadline, 60)))
    except Exception as exc:
        err.append('stop failed: %s' % exc)
        rec['stop_s'] = round(clock() - t0, 1)
        return
    if sess.get('state') != 'stopped':
        err.append('state after stop %r != stopped' % sess.get('state'))
    if native is None and sess.get('native_id'):
        native = sess['native_id']
    while native:
        try:
            gone = native not in live_natives(r, left(clock, deadline, 60))
        except Exception as exc:
            err.append('a list failed: %s' % exc)
            break
        if gone:
            break
        if clock() >= deadline:
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


def run(r, evidence_path, commit, clock=time.monotonic, sleep=time.sleep, out=sys.stdout,
        intake=None, synthetic_ac=False):
    window = {'start': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    problems = list(check_evidence(evidence_path, commit))
    if not intake and not synthetic_ac:
        problems.append('no --intake-task-id and no --synthetic-ac-authorized (acceptance criteria source missing)')
    try:
        before = sample_alerts(r, clock)
    except Exception as exc:
        before = None
        problems.append('alert baseline unavailable: %s' % exc)
    recs = []
    if problems:  # fail closed before touching Hub
        print('ACCEPTANCE FAIL (preconditions): %s' % '; '.join(problems), file=out)
        return 1
    try:
        for i in range(1, COUNT + 1):
            rec = {'n': i, 'ok': False, 'errors': []}
            recs.append(rec)
            try:
                one_start(r, rec, clock, sleep, intake)
            except Exception as exc:  # keep the created id for cleanup
                rec['errors'].append('start crashed: %s' % exc)
            print('start %d/%d id=%s model=%s engine=%s host=%s elapsed=%ss shares(%s)=%s' % (
                i, COUNT, rec.get('id'), rec.get('model'), rec.get('engine'), rec.get('host'), rec.get('elapsed_s'),
                rec.get('shares_source'), json.dumps(rec.get('shares'), sort_keys=True)), file=out, flush=True)
            if rec.get('id') and not rec['errors']:
                try:
                    verify_listing(r, rec, clock)
                except Exception as exc:
                    rec['errors'].append('listing check crashed: %s' % exc)
            if rec['errors'] and (not rec.get('id') or not rec.get('model')):
                break  # consecutive means a failed start ends the run
    finally:
        for rec in recs:  # only sessions this run created, always
            try:
                stop_one(r, rec, clock, sleep)
            except Exception as exc:
                rec['errors'].append('stop crashed: %s' % exc)
    window['end'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    post_problems = list(check_evidence(evidence_path, commit))
    try:
        new = sample_alerts(r, clock) - before
        if new:
            post_problems.append('dev alerts during run: %s' % ', '.join(sorted(new)))
    except Exception as exc:
        post_problems.append('post-run alert sample unavailable: %s' % exc)
    for rec in recs:
        rec.pop('row', None)
    summary = summarize(recs, post_problems)
    summary.update(commit=commit, window=window, starts=recs,
                   acceptance_criteria=('intake ' + intake) if intake else 'server-synthetic (explicitly authorized)')
    print(json.dumps(summary, indent=2, sort_keys=True, default=str), file=out)
    print('ACCEPTANCE %s' % ('PASS' if summary['ok'] else 'FAIL'), file=out)
    return 0 if summary['ok'] else 1


def main(argv=None, runner=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live', action='store_true', help='really start ten sessions (also needs HUB_ACCEPTANCE_READY=1)')
    ap.add_argument('--evidence', help='JSON: canary/ci_e2e (+_commit)')
    ap.add_argument('--commit', help='deployed main commit the evidence must match')
    ap.add_argument('--alerts-cmd', help='command printing JSON {"alerts":[{"id":..}]} of current dev@ alerts; '
                                         'sampled before and after the run')
    ap.add_argument('--intake-task-id', help='intake task with acceptance criteria passed to every start')
    ap.add_argument('--synthetic-ac-authorized', action='store_true',
                    help='explicit authorization to rely on the server-fabricated synthetic criterion')
    args = ap.parse_args(argv)
    if not args.live or os.environ.get('HUB_ACCEPTANCE_READY') != '1':
        print('refusing: live run needs --live and HUB_ACCEPTANCE_READY=1 (orchestrator readiness)', file=sys.stderr)
        return 2
    return run(runner or Runner(alerts_cmd=args.alerts_cmd), args.evidence, args.commit,
               intake=args.intake_task_id, synthetic_ac=args.synthetic_ac_authorized)


if __name__ == '__main__':
    sys.exit(main())
