"""M14b slice2 maintained stdlib host status collector.

Reads ONLY registered/linked sessions (operator-allowlisted workspaces) and
operator-allowlisted report paths. Bounded native captures; session
limit/context/draft states come ONLY from explicit structured markers (never
parsed from transcripts, terminals or screens), headlines are first-heading
only, sanitized (control chars stripped, token shapes redacted, bounded).
Raw transcripts, tokens and screens are never emitted and never sent to Hub.

Standalone entrypoint: ``python3 -m agent_hub_host.status_collect --once``.
Service plumbing that calls this collector belongs to head; this module has
no Hub side effects unless ``--submit`` is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MAX_OBSERVATIONS = 200
MAX_FILE_READ = 8192
MAX_HEADLINE = 240
MAX_NOTE = 240

SESSION_STATES = ('working', 'waiting', 'context_low', 'limit_hit', 'draft_pending', 'idle', 'unknown')
VERDICTS = ('done', 'attention', 'unknown')

CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202e\ufeff]')
TOKEN_RES = [
    re.compile(r'hub[tr]_[A-Za-z0-9_-]{8,}'),
    re.compile(r'gh[pousr]_[A-Za-z0-9]{8,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'xox[bpras]-[A-Za-z0-9-]+'),
    re.compile(r'Bearer\s+[A-Za-z0-9._~+/-]+', re.IGNORECASE),
]


def sanitize(text: Any, limit: int) -> str:
    out = '' if text is None else (text if isinstance(text, str) else str(text))
    out = CONTROL_RE.sub('', out)
    for pattern in TOKEN_RES:
        out = pattern.sub('[redacted]', out)
    return out if len(out) <= limit else out[:limit]


def _real_under(path: str, roots: list) -> bool:
    """Canonical realpath must sit under an allowlisted root (no escapes)."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        try:
            anchor = os.path.realpath(root)
        except OSError:
            continue
        if real == anchor or real.startswith(anchor + os.sep):
            return True
    return False


def load_allowlist(path: str) -> dict:
    """Operator allowlist only. Absent/invalid file fails closed (no collection)."""
    with open(path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError('allowlist must be a JSON object')
    for key in ('session_roots', 'report_roots', 'sessions', 'reports'):
        if key in data and not isinstance(data[key], list):
            raise ValueError('allowlist %s must be a list' % key)
    return data


def classify_session(entry: dict) -> dict:
    """Conservative state from explicit structured markers only.

    Free-text/transcript fields are NEVER inspected: anything not carried by
    an explicit boolean marker degrades to unknown, never inferred.
    Precedence: limit > context > draft > working > waiting > waiting-idle.
    """
    if not isinstance(entry, dict):
        return {'state': 'unknown'}
    if entry.get('limit_exhausted') is True:
        return {'state': 'limit_hit'}
    if entry.get('context_low') is True:
        return {'state': 'context_low'}
    if entry.get('draft_pending') is True:
        return {'state': 'draft_pending'}
    if entry.get('working') is True:
        return {'state': 'working'}
    if entry.get('waiting') is True:
        return {'state': 'waiting'}
    if entry.get('idle') is True:
        return {'state': 'idle'}
    return {'state': 'unknown'}


def read_headline(path: str) -> str | None:
    """First markdown heading (or first non-empty line), bounded. None when unreadable."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            chunk = handle.read(MAX_FILE_READ)
    except OSError:
        return None
    for line in chunk.splitlines():
        stripped = line.strip().lstrip('#').strip()
        if stripped:
            return sanitize(stripped, MAX_HEADLINE)
    return None


def collect_once(sessions: Any, allowlist: dict, machine_id: str, observed_at: str) -> dict:
    """Pure collection round. Returns {'observations': [...], 'skipped': [...]}."""
    observations: list = []
    skipped: list = []
    if not isinstance(sessions, list) or not isinstance(allowlist, dict):
        return {'observations': [], 'skipped': [{'reason': 'invalid_input'}]}
    session_roots = allowlist.get('session_roots') or []
    report_roots = allowlist.get('report_roots') or []
    registered = allowlist.get('sessions') or []
    registered_ids = {s.get('native_id') for s in registered if isinstance(s, dict) and s.get('native_id')}
    use_registry = bool(registered)

    by_native: dict = {}
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        native_id = entry.get('native_id')
        workspace = entry.get('workspace')
        if not native_id or not workspace:
            skipped.append({'native_id': native_id, 'reason': 'missing_identity_or_workspace'})
            continue
        if use_registry and native_id not in registered_ids:
            skipped.append({'native_id': native_id, 'reason': 'unregistered_session'})
            continue
        if not _real_under(str(workspace), [str(r) for r in session_roots]):
            skipped.append({'native_id': native_id, 'reason': 'workspace_outside_allowlist'})
            continue
        by_native[native_id] = entry

    for native_id, entry in sorted(by_native.items()):
        classified = classify_session(entry)
        payload: dict = {'state': classified['state']}
        if isinstance(entry.get('note'), str) and entry['note'].strip():
            payload['note'] = sanitize(entry['note'], MAX_NOTE)
        observations.append({'machine_id': machine_id, 'native_id': native_id,
                             'kind': 'session', 'observed_at': observed_at, 'payload': payload})

    reports = allowlist.get('reports') or []
    for ref in reports:
        if not isinstance(ref, dict) or not ref.get('path') or not ref.get('for_native_id'):
            skipped.append({'report': ref.get('path') if isinstance(ref, dict) else None,
                            'reason': 'invalid_report_entry'})
            continue
        if ref['for_native_id'] not in by_native:
            skipped.append({'report': ref['path'], 'reason': 'report_session_not_collected'})
            continue
        if not _real_under(str(ref['path']), [str(r) for r in report_roots]):
            skipped.append({'report': ref['path'], 'reason': 'report_outside_allowlist'})
            continue
        headline = read_headline(str(ref['path']))
        if not headline:
            skipped.append({'report': ref['path'], 'reason': 'headline_unreadable'})
            continue
        verdict = ref.get('verdict', 'unknown')
        payload = {'headline': headline, 'verdict': verdict if verdict in VERDICTS else 'unknown'}
        observations.append({'machine_id': machine_id, 'native_id': ref['for_native_id'],
                             'kind': 'report', 'observed_at': observed_at, 'payload': payload})

    return {'observations': observations[:MAX_OBSERVATIONS], 'skipped': skipped}


def submit(observations: list, base_url: str, token: str, timeout: int = 20) -> list:
    """POST each observation to /api/status/observations. Bounded, explicit, no retries.

    Transport is the maintained ``api.safe_http_json`` (no-redirect opener,
    ``agent-hub-host/1`` UA, bounded JSON response). Errors never carry the
    token, the request URL, or any raw response body.
    """
    from . import api as _api

    capped = observations[:MAX_OBSERVATIONS] if isinstance(observations, list) else []
    try:
        _api.check_url(base_url)
    except Exception:
        return [{'status': None, 'error': 'invalid_base_url'} for _ in capped]
    if not isinstance(token, str) or not token:
        return [{'status': None, 'error': 'missing_token'} for _ in capped]
    url = base_url.rstrip('/') + '/api/status/observations'
    results = []
    for obs in capped:
        try:
            body = json.dumps(obs).encode('utf-8')
        except (TypeError, ValueError):
            results.append({'status': None, 'error': 'unserializable_observation'})
            continue
        if len(body) > 64 * 1024:
            results.append({'status': None, 'error': 'observation_too_large'})
            continue
        req = urllib.request.Request(url, data=body, method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + token})
        try:
            data = _api.safe_http_json(req, timeout=timeout)
            results.append({'status': 200, 'body': data})
        except urllib.error.HTTPError as exc:
            try:
                code = int(exc.code)
            except (TypeError, ValueError):
                code = None
            try:
                exc.close()
            except Exception:
                pass
            results.append({'status': code, 'error': 'http_error'})
        except Exception as exc:  # noqa: BLE001 - surfaced as data, never raised
            results.append({'status': None, 'error': sanitize(type(exc).__name__, 64) or 'transport_failed'})
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='status_collect', description='M14b allowlisted host status collector')
    parser.add_argument('--once', action='store_true', required=True, help='Run a single collection round')
    parser.add_argument('--allowlist', default=None, help='Operator allowlist JSON path (explicit preferred; default sibling journal/status-allowlist.json)')
    parser.add_argument('--sessions-json', default=None, help='Prepared structured native session list (file or - for stdin). Omit for real native collection via the aplexer adapter.')
    parser.add_argument('--machine-id', default=None, help='Override machine_id (default: allowlist machine_id)')
    parser.add_argument('--submit', action='store_true', help='POST observations to the Hub intake endpoint')
    return parser


def main(argv: list | None = None) -> dict:
    args = build_parser().parse_args(argv)
    from .status_runtime import resolve_allowlist_path
    allowlist_path = resolve_allowlist_path(args.allowlist)
    try:
        allowlist = load_allowlist(allowlist_path)
    except (OSError, ValueError) as exc:
        print('status_collect: refusing to collect: %s' % exc, file=sys.stderr)
        raise SystemExit(2)
    machine_id = args.machine_id or allowlist.get('machine_id')
    if not machine_id:
        print('status_collect: allowlist carries no machine_id', file=sys.stderr)
        raise SystemExit(2)
    from datetime import datetime, timezone
    if args.sessions_json in (None, ''):
        # Real native collection (default): actual aplexer list via the
        # existing adapter + configured probes, in one rate-limited step.
        # No prepared input, no synthesis.
        from .status_runtime import StatusCollector
        collector = StatusCollector(allowlist=allowlist, machine_id=str(machine_id),
                                    min_interval_s=0)
        result = collector.step(force=True, submit=args.submit)
        if args.submit and result.get('submit') is None and result.get('errors'):
            # submit attempted inside step; errors already recorded.
            pass
        json.dump(result, sys.stdout, indent=2, ensure_ascii=True)
        sys.stdout.write('\n')
        return result
    try:
        if args.sessions_json == '-':
            sessions = json.load(sys.stdin)
        else:
            with open(args.sessions_json, 'r', encoding='utf-8') as handle:
                sessions = json.load(handle)
    except (OSError, ValueError) as exc:
        print('status_collect: cannot read sessions input: %s' % exc, file=sys.stderr)
        raise SystemExit(2)
    observed_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    result = collect_once(sessions, allowlist, str(machine_id), observed_at)
    if args.submit:
        from . import api, auth, config
        cfg = config.load_config()
        base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or cfg.get('base_url')
        api.check_url(base)
        token = os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN') or auth.get_access_token()
        result['submit'] = submit(result['observations'], base, token)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write('\n')
    return result


if __name__ == '__main__':
    main()
