"""Early disk alerting for Hub hosts (85% warn, 92% crit, 24h projected fill).

Stores samples/state on tmpfs (XDG_RUNTIME_DIR or /dev/shm) so alerting still
works when the root filesystem is 100% full, and routes alerts through
oncall-send (which itself falls back to tmpfs on ENOSPC) with optional HTTP
webhook fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_WARN_USED_PCT = 85.0
DEFAULT_CRIT_USED_PCT = 92.0
DEFAULT_FILL_CRIT_HOURS = 24.0
DEFAULT_REPEAT_SEC = 4 * 3600


def default_state_dir() -> Path:
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK):
        return Path(runtime) / 'hub-disk-alert'
    shm = Path('/dev/shm')
    if shm.is_dir() and os.access(shm, os.W_OK):
        return shm / f'hub-disk-alert-{os.getuid()}'
    return Path.home() / '.local' / 'state' / 'hub-disk-alert'


def measure_mount_used_pct(mount: Path) -> tuple[float, int, int]:
    """Return (used_pct, avail_bytes, total_bytes) matching df's Use% semantics."""
    if hasattr(os, 'statvfs'):
        st = os.statvfs(mount)
        frsize = st.f_frsize or st.f_bsize
        used = (st.f_blocks - st.f_bfree) * frsize
        avail = st.f_bavail * frsize
        total = st.f_blocks * frsize
        denom = used + avail
        used_pct = (used * 100.0 / denom) if denom > 0 else 0.0
        return used_pct, avail, total
    usage = shutil.disk_usage(mount)
    used_pct = (usage.used * 100.0 / usage.total) if usage.total > 0 else 0.0
    return used_pct, usage.free, usage.total


def projected_fill_hours(
    samples: list[tuple[float, int]],
    *,
    now_s: float,
    window_sec: float = 6 * 3600,
    min_span_sec: float = 1800,
) -> float | None:
    recent = [(t, a) for t, a in samples if now_s - window_sec <= t <= now_s]
    if len(recent) < 2:
        return None
    span = recent[-1][0] - recent[0][0]
    if span < min_span_sec:
        return None
    n = len(recent)
    mean_t = sum(t for t, _ in recent) / n
    mean_a = sum(a for _, a in recent) / n
    denom = sum((t - mean_t) ** 2 for t, _ in recent)
    if denom <= 0:
        return None
    slope_bps = sum((t - mean_t) * (a - mean_a) for t, a in recent) / denom
    if slope_bps >= 0:
        return None
    cur_avail = recent[-1][1]
    if cur_avail <= 0:
        return 0.0
    return (cur_avail / (-slope_bps)) / 3600.0


def evaluate_disk(
    mount: Path = Path('/'),
    *,
    warn_used_pct: float = DEFAULT_WARN_USED_PCT,
    crit_used_pct: float = DEFAULT_CRIT_USED_PCT,
    fill_crit_hours: float = DEFAULT_FILL_CRIT_HOURS,
    state_dir: Path | None = None,
    now_s: float | None = None,
    force_used_pct: float | None = None,
    force_fill_hours: float | None = None,
) -> dict[str, Any]:
    now_s = time.time() if now_s is None else now_s
    state_dir = state_dir or default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    samples_file = state_dir / 'samples.jsonl'

    used_pct, avail, total = measure_mount_used_pct(mount)
    if force_used_pct is not None:
        used_pct = float(force_used_pct)

    samples: list[tuple[float, int]] = []
    if samples_file.is_file():
        for line in samples_file.read_text(encoding='utf-8').splitlines()[-200:]:
            try:
                rec = json.loads(line)
                samples.append((float(rec['ts']), int(rec['avail'])))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
    samples.append((now_s, avail))
    samples = [(t, a) for t, a in samples if now_s - 7 * 3600 <= t <= now_s + 60][-200:]
    samples_file.write_text(
        ''.join(json.dumps({'ts': t, 'avail': a}) + '\n' for t, a in samples),
        encoding='utf-8',
    )

    fill_h = force_fill_hours if force_fill_hours is not None else projected_fill_hours(samples, now_s=now_s)
    level = 'OK'
    reasons: list[str] = []
    if used_pct >= crit_used_pct:
        level = 'CRIT'
        reasons.append(f'used {used_pct:.1f}% >= crit {crit_used_pct:.0f}%')
    elif used_pct >= warn_used_pct:
        level = 'WARN'
        reasons.append(f'used {used_pct:.1f}% >= warn {warn_used_pct:.0f}%')

    if fill_h is not None and fill_h <= fill_crit_hours:
        level = 'CRIT'
        reasons.append(f'projected fill in {fill_h:.1f}h <= {fill_crit_hours:.0f}h')

    return {
        'host': socket.gethostname(),
        'mount': str(mount),
        'level': level,
        'used_pct': round(used_pct, 2),
        'avail_gb': round(avail / (1024 ** 3), 2),
        'total_gb': round(total / (1024 ** 3), 2),
        'fill_hours': None if fill_h is None else round(fill_h, 2),
        'reasons': reasons,
        'forced': force_used_pct is not None or force_fill_hours is not None,
    }


def dispatch_alert(
    report: dict[str, Any],
    *,
    oncall_bin: str | None = None,
    http_url: str | None = None,
) -> dict[str, Any]:
    msg = (
        f"[DISK {report['level']}] {report['host']} mount={report['mount']} "
        f"used={report['used_pct']}% avail={report['avail_gb']}GiB/{report['total_gb']}GiB "
        f"({'; '.join(report['reasons']) or 'ok'})"
    )
    if report.get('forced'):
        msg += ' [FORCED_THRESHOLD_TEST]'

    cmd = oncall_bin or os.environ.get('HUB_DISK_ALERT_ONCALL_BIN') or str(Path.home() / 'bin' / 'oncall-send')
    if os.path.isfile(cmd) and os.access(cmd, os.X_OK):
        proc = subprocess.run([cmd, msg], capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode in (0, 2):
            return {'ok': True, 'via': 'oncall-send', 'rc': proc.returncode, 'stdout': proc.stdout.strip()}

    url = http_url or os.environ.get('HUB_DISK_ALERT_HTTP_URL')
    if url:
        req = urllib.request.Request(
            url,
            data=json.dumps({'text': msg, 'report': report}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {'ok': 200 <= resp.status < 300, 'via': 'http', 'status': resp.status}

    return {'ok': False, 'via': 'none', 'message': msg}


def run_check(
    *,
    mount: Path = Path('/'),
    state_dir: Path | None = None,
    force_used_pct: float | None = None,
    force_fill_hours: float | None = None,
    oncall_bin: str | None = None,
    http_url: str | None = None,
    now_s: float | None = None,
) -> dict[str, Any]:
    now_s = time.time() if now_s is None else now_s
    state_dir = state_dir or default_state_dir()
    report = evaluate_disk(
        mount, state_dir=state_dir, now_s=now_s,
        force_used_pct=force_used_pct, force_fill_hours=force_fill_hours,
    )
    state_file = state_dir / 'alert_state.json'
    prev: dict[str, Any] = {}
    if state_file.is_file():
        try:
            prev = json.loads(state_file.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            prev = {}

    delivery: dict[str, Any] | None = None
    if report['level'] in ('WARN', 'CRIT'):
        last_sent = float(prev.get('last_sent_ts', 0))
        escalated = prev.get('level') == 'WARN' and report['level'] == 'CRIT'
        if report['forced'] or escalated or prev.get('level') == 'OK' or not prev or (now_s - last_sent >= DEFAULT_REPEAT_SEC):
            delivery = dispatch_alert(report, oncall_bin=oncall_bin, http_url=http_url)
            if delivery.get('ok') and not report['forced']:
                state_file.write_text(json.dumps({'level': report['level'], 'last_sent_ts': now_s}), encoding='utf-8')
    elif prev.get('level') in ('WARN', 'CRIT'):
        state_file.write_text(json.dumps({'level': 'OK', 'last_sent_ts': now_s}), encoding='utf-8')

    return {'report': report, 'delivery': delivery}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Early disk check and tmpfs-safe oncall alert')
    parser.add_argument('--mount', type=Path, default=Path('/'))
    parser.add_argument('--force-used-pct', type=float, default=None)
    parser.add_argument('--force-fill-hours', type=float, default=None)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    res = run_check(
        mount=args.mount,
        force_used_pct=args.force_used_pct,
        force_fill_hours=args.force_fill_hours,
    )
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        r = res['report']
        print(f"level={r['level']} mount={r['mount']} used={r['used_pct']}% avail={r['avail_gb']}GiB")
    return 1 if res['report']['level'] in ('WARN', 'CRIT') and not res['report']['forced'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
