"""RMTHZ host hook: reap labeled test containers by TTL; legacy never-started guard retained.

Two paths share one safety core:
- legacy: only remove never-started (status=created, zero StartedAt) agent
  containers after the Hub confirms the linked session is stopped.
- ttl: remove any non-running container carrying a test label
  (agent.session=<id> or pocketshell.test=1) older than N hours
  (configurable, default 6h).

Run periodically as the host operator with Docker access and Hub credentials.
Default is dry-run; no Docker force/volume removal is used. Lookup failures,
Coolify labels, unlabeled containers, running containers and unknown session
states always retain. Logs contain only IDs/actions.
"""
import argparse
from datetime import datetime, timezone
import json
import re
import subprocess

SESSION = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z')
DEFAULT_MAX_AGE_HOURS = 6.0


def eligible(container, session_id):
    labels = container.get('Config', {}).get('Labels') or {}
    state = container.get('State') or {}
    return (bool(SESSION.fullmatch(session_id))
            and labels.get('agent.session') == session_id
            and not any(k.lower().startswith(('coolify', 'com.coolify')) for k in labels)
            and state.get('Status') == 'created'
            and state.get('StartedAt', '').startswith('0001-01-01T00:00:00'))


def has_test_label(labels) -> bool:
    """True for labeled test containers; unlabeled and Coolify never qualify."""
    if not isinstance(labels, dict):
        return False
    if any(k.lower().startswith(('coolify', 'com.coolify')) for k in labels):
        return False
    sid = labels.get('agent.session')
    if isinstance(sid, str) and bool(SESSION.fullmatch(sid)):
        return True
    marker = labels.get('pocketshell.test')
    if isinstance(marker, str) and marker.strip().lower() in ('1', 'true', 'yes'):
        return True
    return False


def parse_created_at(value) -> float | None:
    """Parse docker Created timestamp to epoch seconds; None when unknown."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        # Docker zero time / legacy formats fail closed (unknown age).
        return None


def container_age_hours(container, now: float | None = None) -> float | None:
    created = None
    if isinstance(container, dict):
        created = container.get('Created') or (container.get('Config') or {}).get('Created')
    ts = parse_created_at(created) if isinstance(created, str) else None
    if ts is None:
        return None
    ref = now if isinstance(now, (int, float)) else datetime.now(timezone.utc).timestamp()
    return max(0.0, (ref - ts) / 3600.0)


def eligible_ttl(container, now: float | None = None, max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> bool:
    """TTL guard: labeled test container, non-running, older than N hours."""
    labels = (container.get('Config', {}).get('Labels') if isinstance(container, dict) else None) or {}
    if not has_test_label(labels):
        return False
    state = container.get('State') or {}
    status = str(state.get('Status') or '').lower()
    if status in ('running', 'restarting', 'paused'):
        return False
    age = container_age_hours(container, now)
    if age is None:
        return False
    try:
        threshold = float(max_age_hours)
    except (TypeError, ValueError):
        threshold = DEFAULT_MAX_AGE_HOURS
    return age >= threshold


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=30).stdout


def _list_ids(run, extra_filters) -> list[str]:
    try:
        out = run(['docker', 'ps', '-aq', *extra_filters]).split()
    except Exception:
        return []
    return [c for c in out if re.fullmatch(r'[0-9a-f]{12,64}', c)]


def _hub_session_state(run, hub_bin, sid):
    try:
        session = json.loads(run([hub_bin, 'sessions', 'show', sid]))
    except Exception:
        return None
    if not isinstance(session, dict) or session.get('id') != sid:
        return None
    return session.get('state')


def sweep(apply=False, hub_bin='hub', run=command, max_age_hours=DEFAULT_MAX_AGE_HOURS, now=None):
    # Legacy never-started path (exact ps filters preserved for back-compat).
    ids = run(['docker', 'ps', '-aq', '--filter', 'status=created',
               '--filter', 'label=agent.session']).split()
    seen: set[str] = set()
    removed: set[str] = set()
    for cid in ids:
        if not re.fullmatch(r'[0-9a-f]{12,64}', cid):
            continue
        seen.add(cid)
        try:
            container = json.loads(run(['docker', 'inspect', cid]))[0]
            sid = (container.get('Config', {}).get('Labels') or {}).get('agent.session', '')
            if not eligible(container, sid):
                continue
            session = json.loads(run([hub_bin, 'sessions', 'show', sid]))
            # 'failed' may still carry an uncertain native process. Only the
            # Hub's confirmed stopped state authorizes this host cleanup.
            if session.get('id') != sid or session.get('state') != 'stopped':
                continue
            print(f'dry-run container={cid} session={sid} action=remove-created', flush=True)
            if apply:
                current = json.loads(run(['docker', 'inspect', cid]))[0]
                if eligible(current, sid):
                    run(['docker', 'rm', cid])
                    removed.add(cid)
                    print(f'removed container={cid} session={sid}', flush=True)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, TypeError):
            print(f'retained container={cid} reason=lookup-or-removal-failed', flush=True)
    # TTL path: labeled test containers older than N hours, non-running only.
    # TTL re-evaluates even legacy-seen containers (legacy retains on Hub
    # lookup failure); already-removed legacy containers are skipped.
    ttl_ids: list[str] = []
    ttl_seen: set[str] = set()
    for filt in (['--filter', 'label=agent.session'], ['--filter', 'label=pocketshell.test']):
        for cid in _list_ids(run, filt):
            if cid in removed or cid in ttl_seen:
                continue
            ttl_ids.append(cid)
            ttl_seen.add(cid)
    try:
        threshold = float(max_age_hours)
    except (TypeError, ValueError):
        threshold = DEFAULT_MAX_AGE_HOURS
    for cid in ttl_ids:
        try:
            container = json.loads(run(['docker', 'inspect', cid]))[0]
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, TypeError):
            print(f'retained container={cid} reason=lookup-or-removal-failed', flush=True)
            continue
        labels = (container.get('Config', {}).get('Labels') if isinstance(container, dict) else None) or {}
        if not has_test_label(labels):
            continue
        state = container.get('State') or {}
        status = str(state.get('Status') or '').lower()
        if status in ('running', 'restarting', 'paused'):
            print(f'retained container={cid} reason=running', flush=True)
            continue
        age = container_age_hours(container, now)
        if age is None or age < threshold:
            continue
        sid = labels.get('agent.session', '') if isinstance(labels.get('agent.session'), str) else ''
        # When an agent.session label names a Hub session, only a confirmed
        # stopped session authorizes TTL removal; live or unknown sessions
        # retain (fail-closed). Pure pocketshell.test containers skip Hub lookup.
        if sid and bool(SESSION.fullmatch(sid)):
            hub_state = _hub_session_state(run, hub_bin, sid)
            if hub_state is None:
                print(f'retained container={cid} reason=hub-unknown', flush=True)
                continue
            elif hub_state != 'stopped':
                print(f'retained container={cid} reason=session-{hub_state}', flush=True)
                continue
            else:
                verb = 'removing' if apply else 'dry-run'
                print(f'{verb} container={cid} session={sid} action=remove-ttl age_h={age:.1f}', flush=True)
        else:
            verb = 'removing' if apply else 'dry-run'
            print(f'{verb} container={cid} session={sid or "pocketshell.test"} action=remove-ttl age_h={age:.1f}', flush=True)
        if apply:
            try:
                current = json.loads(run(['docker', 'inspect', cid]))[0]
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, TypeError):
                print(f'retained container={cid} reason=lookup-or-removal-failed', flush=True)
                continue
            if not eligible_ttl(current, now, threshold):
                print(f'retained container={cid} reason=recheck-failed', flush=True)
                continue
            if sid and bool(SESSION.fullmatch(sid)):
                hub_state = _hub_session_state(run, hub_bin, sid)
                if hub_state != 'stopped':
                    print(f'retained container={cid} reason=session-{hub_state or "hub-unknown"}', flush=True)
                    continue
            try:
                run(['docker', 'rm', cid])
            except (subprocess.SubprocessError, OSError):
                print(f'retained container={cid} reason=lookup-or-removal-failed', flush=True)
                continue
            print(f'removed container={cid} session={sid or "pocketshell.test"}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Actually remove (default is dry-run).')
    parser.add_argument('--hub-bin', default='hub')
    parser.add_argument('--max-age-hours', type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help='TTL in hours for labeled test containers (default 6h).')
    parser.add_argument('--dry-run', action='store_true', help='Explicit dry-run (default); overrides --apply.')
    args = parser.parse_args()
    apply = args.apply and not args.dry_run
    sweep(apply, args.hub_bin, max_age_hours=args.max_age_hours)


if __name__ == '__main__':
    main()
