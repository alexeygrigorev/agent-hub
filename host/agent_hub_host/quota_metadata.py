"""Shared operator quota projection; no dispatcher import or provider I/O."""
from __future__ import annotations

import datetime as dt


def quota_metadata(raw, service, profile=None, account=None):
    """Reuse the maintained quota contract; never reimplement its failure/absence semantics.

    The quse probe key is the provider selector (``codex`` or ``codex-orin``),
    not the aplexer profile (``hub-codex-orin``) nor the service alone. Try the
    profile, then the account label, then the Orin home key (only when the
    caller addresses the Orin account), then the service so the Orin home
    never falls back to the whole raw dict (which sanitizes to status unknown
    with empty windows).
    """
    from .quota_snapshot import sanitize_provider, format_iso_utc, COLLECTOR_VERSION_REGEX
    if not isinstance(raw, dict):
        route = {}
    else:
        candidates: list[str] = []
        if isinstance(profile, str) and profile:
            candidates.append(profile)
        if isinstance(account, str) and account:
            candidates.append(account)
        # Second-Codex-home key only when the caller actually addresses the
        # Orin account: a generic service-only lookup must keep resolving the
        # shared `codex` route as before.
        if service == 'codex' and (account == 'codex-orin' or profile in ('hub-codex-orin', 'codex-orin')):
            candidates.append('codex-orin')
        if isinstance(service, str) and service:
            candidates.append(service)
        route = {}
        for key in candidates:
            candidate = raw.get(key)
            if isinstance(candidate, dict):
                route = candidate
                break
        else:
            route = raw if ('status' in raw or 'windows' in raw) else {}
    if not isinstance(route, dict): route = {}
    details = route.get('details') if isinstance(route.get('details'), dict) else {}
    now = dt.datetime.now(dt.timezone.utc)
    clean = sanitize_provider(service, route, now)
    status = 'ok' if clean['status'] == 'ok' else 'unknown'
    explicit_timestamp = 'observed_at' in route or 'observed_at' in details
    if explicit_timestamp and (route.get('observed_at') is None and details.get('observed_at') is None):
        status = 'unknown'
        observed_at = None
    else:
        observed_at = clean['observed_at'] if explicit_timestamp else format_iso_utc(now)
    if explicit_timestamp and observed_at is None: status = 'unknown'
    # Extra conservative handling of explicitly unknown freshness and optional stale flags.
    if any('stale' in obj and obj['stale'] is not False for obj in (route, details)):
        status = 'unknown'
    windows = route.get('windows')
    if isinstance(windows, dict) and any(isinstance(w, dict) and 'stale' in w and w['stale'] is not False for w in windows.values()):
        status = 'unknown'
    window_keys = ('present', 'percent_remaining', 'reset_at', 'rolling', 'error',
                   'credits_remaining', 'spend_used', 'spend_limit', 'unit', 'profile')
    quota = {'status': status, 'observed_at': observed_at, 'details': clean['details'],
             'windows': {name: {k: w.get(k) for k in window_keys} for name, w in clean['windows'].items()}}
    collector_version = raw.get('collector_version') if isinstance(raw, dict) else None
    quota['collector_version'] = collector_version if isinstance(collector_version, str) and COLLECTOR_VERSION_REGEX.match(collector_version) else None
    return quota
