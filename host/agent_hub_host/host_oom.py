"""Host OOM evidence from kernel constraint records, never vmstat's victim count.

memory.events is hierarchical and oom_kill counts victims even when a different
limit caused the kill. The kernel's oom_memcg instead names the limiting cgroup.
Only the kernel journal is queried; arbitrary session output is never parsed.
"""
import json
import re
import subprocess
import time

_seen = set()


def is_host_oom(message):
    if not isinstance(message, str) or not message.startswith('oom-kill:'):
        return False
    fields = dict(re.findall(r'(?:^|[, :])([a-z_]+)=([^,\s]+)', message))
    if fields.get('constraint') in {'CONSTRAINT_NONE', 'CONSTRAINT_CPUSET', 'CONSTRAINT_MEMORY_POLICY'}:
        return True  # Global OOM, including victims in a session cgroup.
    if fields.get('constraint') != 'CONSTRAINT_MEMCG':
        return False
    limit = fields.get('oom_memcg', '')
    if not limit.startswith('/') or '..' in limit.split('/'):
        return False
    parts = limit.strip('/').split('/')
    # Descendants of a session/workload remain isolated, regardless of victim.
    if any(p.startswith(('aplexer-workload-', 'session-', 'launcher-'))
           or p.endswith('.scope') for p in parts):
        return False
    # Shared slices and the user manager are host-wide limits. An arbitrary
    # service/container subtree is not evidence that the host is choking.
    return limit == '/' or all(p.endswith('.slice') or re.fullmatch(r'user@\d+\.service', p)
                               for p in parts)


def read_host_oom_delta(*, query=None, now=None):
    """Count new verified host events from a bounded overlapping journal window.

    A five minute lookback deliberately matches the default choking cooldown.
    Deduplicate overlapping polls so healthy heartbeats do not renew cooldown.
    Missing access is unknown, never inferred from the unscoped vmstat counter.
    """
    global _seen
    now = time.time() if now is None else now
    # The host user cannot read the system journal on Hetzner. Noninteractive
    # read-only sudo uses the existing host policy; never prompt or change it.
    argv = ['sudo', '-n', 'journalctl', '-k', '--boot', '--since', '@' + str(now - 300),
            '--no-pager', '--output=json', '--output-fields=MESSAGE,__REALTIME_TIMESTAMP',
            '--lines=256', '--grep=^oom-kill:']
    try:
        if query:
            raw = query(argv)
        else:
            result = subprocess.run(argv, capture_output=True, timeout=2, check=False)
            # journalctl --grep uses 1 for an empty successful search. Sudo,
            # access and command errors carry stderr and must remain unknown.
            if result.returncode != 0 and not (
                    result.returncode == 1 and not result.stdout and not result.stderr):
                return None
            raw = result.stdout
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        if len(raw) > 262144:
            return None
        events = set()
        for line in raw.splitlines():
            row = json.loads(line)
            stamp = int(row['__REALTIME_TIMESTAMP']) / 1000000
            if now - 300 <= stamp <= now and is_host_oom(row.get('MESSAGE')):
                events.add((stamp, row['MESSAGE']))
        count = len(events - _seen)
        _seen = events
        return count
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return None
