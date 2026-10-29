"""Bounded terminal/missing Hub reconciliation through retaining native stops.

The Hub must positively authorize each exact incarnation. Catalog absence,
transport errors, protected sessions and interactive sessions never authorize
an effect. Uncertain effects are reconciled, never blindly repeated.
"""
import json
import time
from pathlib import Path

from . import private_fs
from .native_sessions import NativeTarget, SessionUncertain, reconcile_stop_oracle, stop_session


FIELDS = ('native_id', 'tag', 'workspace', 'native_created_at_ms', 'engine')
TERMINAL = frozenset(('stopped', 'cancelled', 'failed', 'finished', 'exited', 'refused', 'missing'))


def identity(row):
    value = {'native_id': row.get('id'), 'tag': row.get('tag'), 'workspace': row.get('workspace'),
             'native_created_at_ms': row.get('created_at_ms'), 'engine': row.get('engine')}
    if (not all(isinstance(value[k], str) and value[k] for k in FIELDS if k != 'native_created_at_ms')
            or type(value['native_created_at_ms']) is not int or value['native_created_at_ms'] <= 0):
        return None
    try:
        NativeTarget(session_id=value['native_id'], tag=value['tag'], workspace=value['workspace'],
                     created_at_ms=value['native_created_at_ms'], engine=value['engine']).validated()
    except Exception:
        return None
    return value


def authorized(fact, target):
    return (isinstance(fact, dict) and all(fact.get(k) == target[k] for k in FIELDS)
            and fact.get('managed') is True and fact.get('mode') == 'headless'
            and fact.get('protected') is False and fact.get('state') in TERMINAL)


def reconcile(request, rows, journal_dir, *, executable='aplexer', max_stops=4, max_checks=4):
    """One finite pass. Never removes native records, worktrees or files."""
    summary = {'reaped': 0, 'pending': 0, 'skipped': 0}
    if not journal_dir or not isinstance(rows, list):
        return summary
    path = Path(journal_dir) / 'session-reaper.json'
    try:
        with private_fs.exclusive_lock(Path(journal_dir) / 'session-reaper.lock'):
            try:
                state = json.loads(private_fs.read_bytes(path, 512 * 1024))
                if not isinstance(state, dict) or len(state) > 1000:
                    return summary
            except FileNotFoundError:
                state = {}

            def save():
                private_fs.atomic_write(path, json.dumps(state).encode(), 512 * 1024)

            bounded = rows  # Work per pass is bounded; the rotating scan must cover the whole census.
            cursor = state.get('_cursor', 0)
            if type(cursor) is not int or cursor < 0:
                return summary
            checks = 0
            deadline = time.monotonic() + 5
            for offset in range(len(bounded)):
                if (checks >= max_checks or time.monotonic() >= deadline
                        or summary['reaped'] + summary['pending'] >= max_stops):
                    break
                index = (cursor + offset) % len(bounded)
                state['_cursor'] = (index + 1) % len(bounded)
                row = bounded[index]
                if not isinstance(row, dict):
                    continue
                target = identity(row)
                if target is None:
                    continue
                key = target['native_id'] + ':' + str(target['native_created_at_ms'])
                prior = state.get(key)
                if prior:
                    if not isinstance(prior, dict) or prior.get('native') != target:
                        summary['skipped'] += 1
                        continue
                    if prior.get('confirmed') is True:
                        continue
                    oracle = prior.get('oracle')
                    if isinstance(oracle, dict):
                        checks += 1
                        confirmed, _ = reconcile_stop_oracle(executable, oracle, prior.get('kill', {}))
                        if confirmed:
                            prior['confirmed'] = True
                            save()
                            summary['reaped'] += 1
                            continue
                    # A crash between stop and receipt persistence is also
                    # uncertain. Never infer a failed start and send a new kill.
                    summary['pending'] += 1
                    continue
                if summary['reaped'] + summary['pending'] >= max_stops or len(state) >= 1000:
                    break
                if row.get('containment_empty') is True and (row.get('phase') or row.get('state')) in TERMINAL:
                    continue  # Already naturally empty: there is no tree left to reap.
                # Fresh server authority is read just before each effect. The
                # endpoint checks aliases, protections and missing Hub rows.
                checks += 1
                receipt = request('POST', '/api/sessions/host-reconcile', {'native': target})
                if not isinstance(receipt, dict) or not authorized(receipt.get('session'), target):
                    summary['skipped'] += 1
                    continue
                state[key] = {'native': target, 'confirmed': False}
                save()  # Fail closed if durable intent cannot be recorded.
                native = NativeTarget(session_id=target['native_id'], tag=target['tag'],
                                      workspace=target['workspace'], created_at_ms=target['native_created_at_ms'],
                                      engine=target['engine'])
                try:
                    stopped = stop_session(executable, native)
                    if stopped.state == 'stopped':
                        state[key]['confirmed'] = True
                        summary['reaped'] += 1
                    else:
                        summary['pending'] += 1
                except SessionUncertain as exc:
                    if isinstance(getattr(exc, 'oracle', None), dict):
                        state[key]['oracle'] = exc.oracle
                        state[key]['kill'] = getattr(exc, 'kill', {})
                    summary['pending'] += 1
                save()
            save()  # Persist scan progress; large catalogs cannot starve later rows.
    except Exception:
        # Fixed counters only. Native errors/HTTP bodies can contain secrets.
        summary['skipped'] += 1
    return summary
