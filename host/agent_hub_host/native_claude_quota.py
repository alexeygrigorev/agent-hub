"""Authoritative managed Claude quota probe reading CLAUDE_CODE_OAUTH_TOKEN.

Protocol: Anthropic Claude Code 2.1.x OAuth usage API and rate-limit headers.
Does not synthesize quota, bypass gates, or fall back to paid API billing.
Probe produces authoritative actual provider usage observations for portable
token profiles. If the provider forbids read-only usage checks on inference-only
tokens lacking user:profile scope, it fails closed cleanly and honestly.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from . import private_fs

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
COUNT_TOKENS_URL = 'https://api.anthropic.com/v1/messages/count_tokens'
MAX_BODY = 131072
DEFAULT_MODEL = 'claude-sonnet-4-6'
# The usage endpoint rate-limits a token after a handful of rapid reads, and
# every managed launch probes twice (launcher plan + run). One successful
# reading is shared per token across processes so a launch burst does not
# turn into 429s and a missing quota source (intake 798f0f8c).
USAGE_CACHE_FILE = 'claude-usage-cache.json'
USAGE_REUSE_S = 60
USAGE_RATE_LIMITED_REUSE_S = 1800
_clock = time.time


def resolve_api_model(selected_model):
    """Map CLI model alias or requested model to exact active API model ID."""
    if not selected_model or not isinstance(selected_model, str):
        return DEFAULT_MODEL
    m = selected_model.strip().lower()
    if m in ('sonnet', 'claude-sonnet'):
        return 'claude-sonnet-4-6'
    if m in ('opus', 'claude-opus'):
        return 'claude-opus-4-8'
    if m in ('haiku', 'claude-haiku'):
        return 'claude-haiku-4-5'
    return selected_model.strip()

# Reasonable epoch timestamp boundaries for reset headers (2024 to 2035)
MIN_EPOCH_RESET = 1_700_000_000.0
MAX_EPOCH_RESET = 2_051_222_400.0


class ProbeError(ValueError):
    """Contains a fixed safe code only, never a provider body or credential."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError('native_claude_redirect_refused')


def _json_bytes(raw):
    if len(raw) > MAX_BODY:
        raise ProbeError('native_claude_response_too_large')
    try:
        value = json.loads(raw)
    except Exception:
        raise ProbeError('native_claude_invalid_response') from None
    if not isinstance(value, dict):
        raise ProbeError('native_claude_invalid_response')
    return value


def _token(value):
    if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ProbeError('native_claude_login_required')
    return value.strip()


def _extract_token(bundle, dest):
    env = bundle.get('env') if isinstance(bundle, dict) else {}
    token = env.get('CLAUDE_CODE_OAUTH_TOKEN') if isinstance(env, dict) else None
    if token:
        return _token(token)
    dest_path = Path(dest) if dest else None
    if dest_path:
        creds_file = dest_path / 'home' / '.claude' / '.credentials.json'
        if creds_file.is_file() and not creds_file.is_symlink():
            try:
                raw = private_fs.read_bytes(str(creds_file), MAX_BODY, private=True)
                data = _json_bytes(raw)
                oauth = data.get('claudeAiOauth')
                if isinstance(oauth, dict):
                    tok = oauth.get('accessToken')
                    if tok:
                        return _token(tok)
            except Exception:
                pass
    raise ProbeError('native_claude_login_required')


def _parse_iso_tz(val, now):
    """Parse ISO timestamp, strictly requiring timezone info and future timestamp.

    Never attach UTC to naive timestamps. Reject naive or past reset timestamps.
    """
    if not val or not isinstance(val, str):
        raise ProbeError('native_claude_malformed_windows')
    cleaned = val.replace('Z', '+00:00')
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except Exception:
        raise ProbeError('native_claude_malformed_windows') from None
    if parsed.tzinfo is None:
        raise ProbeError('native_claude_malformed_windows')
    if parsed < now:
        raise ProbeError('native_claude_malformed_windows')
    return parsed.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _applicable_model_key(selected_model):
    """Map selected model name or alias to applicable usage payload window key."""
    if not selected_model or not isinstance(selected_model, str):
        return None
    m = selected_model.strip().lower()
    if 'sonnet' in m:
        return 'seven_day_sonnet'
    if 'opus' in m:
        return 'seven_day_opus'
    if 'haiku' in m:
        return 'seven_day_haiku'
    return f'seven_day_{m}'


def _parse_window(win_data, now, required_reset=True):
    """Parse a single window dict with utilization and resets_at."""
    if not isinstance(win_data, dict):
        raise ProbeError('native_claude_malformed_windows')
    u = win_data.get('utilization')
    if u is None or isinstance(u, bool) or not isinstance(u, (int, float)) or not math.isfinite(u) or not 0 <= u <= 100:
        raise ProbeError('native_claude_malformed_windows')
    r_raw = win_data.get('resets_at')
    if r_raw is None:
        if float(u) == 0.0 and not required_reset:
            # Native provider inactive window: known 0% used, no active reset
            return {'percent_remaining': 100.0, 'reset_at': None, 'inactive': True}
        raise ProbeError('native_claude_malformed_windows')
    r = _parse_iso_tz(r_raw, now)
    p = max(0.0, min(100.0, round(100.0 - float(u), 2)))
    return {'percent_remaining': p, 'reset_at': r}


def _parse_usage_payload(data, now, selected_model=None):
    """Parse GET /api/oauth/usage response.

    utilization in usage endpoint is a percentage between 0 and 100.
    DO NOT default missing utilization to 0! Both five_hour and seven_day must
    carry valid numeric utilization readings.

    Applicable model windows (e.g. seven_day_sonnet, seven_day_opus) are parsed
    strictly for the requested model; unrelated model windows are ignored.
    If an applicable model window is null in the payload, it is omitted as not applicable.
    """
    five_hour = data.get('five_hour')
    seven_day = data.get('seven_day')
    if not isinstance(five_hour, dict) or not isinstance(seven_day, dict):
        raise ProbeError('native_claude_malformed_windows')

    win_5h = _parse_window(five_hour, now, required_reset=False)
    win_7d = _parse_window(seven_day, now, required_reset=True)

    windows = {
        '5h': win_5h,
        '7d': win_7d,
    }

    # Model-specific windows: match selected model, ignore unrelated models
    applicable_key = _applicable_model_key(selected_model)
    if applicable_key and applicable_key in data:
        val = data[applicable_key]
        if isinstance(val, dict):
            windows[applicable_key] = _parse_window(val, now, required_reset=False)
        elif val is not None:
            raise ProbeError('native_claude_malformed_windows')
        # If val is None: omitted as explicitly not applicable

    limit_reached = any(w['percent_remaining'] <= 0.0 for w in windows.values())
    details = {
        'limit_reached': limit_reached,
        'source': 'native_claude_oauth_usage',
        'managed': True,
    }
    if selected_model:
        details['model'] = selected_model
    sub = data.get('subscription')
    if isinstance(sub, str) and sub:
        details['subscription'] = sub

    return {
        'claude': {
            'status': 'ok',
            'details': details,
            'windows': windows,
        }
    }


def _parse_unified_headers(headers, now, selected_model=None):
    """Parse complete native 5h and 7d headers from response headers.

    Utilization in headers is a fraction 0.0 to 1.0.
    Resets in headers are epoch seconds.
    Both 5h and 7d must be present.
    Reject nonfinite or out-of-range epoch headers, and reject past resets.
    """
    status = headers.get('anthropic-ratelimit-unified-status')
    u5_raw = headers.get('anthropic-ratelimit-unified-5h-utilization')
    r5_raw = headers.get('anthropic-ratelimit-unified-5h-reset')
    u7_raw = headers.get('anthropic-ratelimit-unified-7d-utilization')
    r7_raw = headers.get('anthropic-ratelimit-unified-7d-reset')

    if u5_raw is None or r5_raw is None or u7_raw is None or r7_raw is None:
        raise ProbeError('native_claude_headers_incomplete')

    try:
        u5 = float(u5_raw)
        r5_ts = float(r5_raw)
        u7 = float(u7_raw)
        r7_ts = float(r7_raw)
    except (ValueError, TypeError):
        raise ProbeError('native_claude_headers_incomplete') from None

    if not math.isfinite(u5) or not 0.0 <= u5 <= 1.0 or not math.isfinite(u7) or not 0.0 <= u7 <= 1.0:
        raise ProbeError('native_claude_headers_incomplete')
    if not math.isfinite(r5_ts) or not math.isfinite(r7_ts):
        raise ProbeError('native_claude_headers_incomplete')

    # Range and freshness validation for epoch resets
    now_ts = now.timestamp()
    if not (MIN_EPOCH_RESET <= r5_ts <= MAX_EPOCH_RESET) or r5_ts < now_ts:
        raise ProbeError('native_claude_malformed_windows')
    if not (MIN_EPOCH_RESET <= r7_ts <= MAX_EPOCH_RESET) or r7_ts < now_ts:
        raise ProbeError('native_claude_malformed_windows')

    p5 = max(0.0, min(100.0, round((1.0 - u5) * 100.0, 2)))
    p7 = max(0.0, min(100.0, round((1.0 - u7) * 100.0, 2)))
    r5_iso = dt.datetime.fromtimestamp(r5_ts, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    r7_iso = dt.datetime.fromtimestamp(r7_ts, dt.timezone.utc).isoformat().replace('+00:00', 'Z')

    windows = {
        '5h': {'percent_remaining': p5, 'reset_at': r5_iso},
        '7d': {'percent_remaining': p7, 'reset_at': r7_iso},
    }

    # Model specific unified headers if available
    applicable_key = _applicable_model_key(selected_model)
    if applicable_key:
        hdr_prefix = f'anthropic-ratelimit-unified-{applicable_key.replace("_", "-")}'
        um_raw = headers.get(f'{hdr_prefix}-utilization')
        rm_raw = headers.get(f'{hdr_prefix}-reset')
        if um_raw is not None and rm_raw is not None:
            try:
                um = float(um_raw)
                rm_ts = float(rm_raw)
                if math.isfinite(um) and 0.0 <= um <= 1.0 and math.isfinite(rm_ts) and (MIN_EPOCH_RESET <= rm_ts <= MAX_EPOCH_RESET) and rm_ts >= now_ts:
                    pm = max(0.0, min(100.0, round((1.0 - um) * 100.0, 2)))
                    rm_iso = dt.datetime.fromtimestamp(rm_ts, dt.timezone.utc).isoformat().replace('+00:00', 'Z')
                    windows[applicable_key] = {'percent_remaining': pm, 'reset_at': rm_iso}
            except (ValueError, TypeError):
                raise ProbeError('native_claude_malformed_windows') from None

    limit_reached = (status != 'allowed' or any(w['percent_remaining'] <= 0.0 for w in windows.values()))
    details = {
        'limit_reached': limit_reached,
        'source': 'native_claude_unified_headers',
        'managed': True,
    }
    if selected_model:
        details['model'] = selected_model

    return {
        'claude': {
            'status': 'ok',
            'details': details,
            'windows': windows,
        }
    }


def _extract_selected_model(bundle, model=None):
    if model:
        return model
    args = bundle.get('args', []) if isinstance(bundle, dict) else []
    for flag in ('--model', '-m'):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
    return None


def _usage_cache_path(dest):
    return Path(dest) / USAGE_CACHE_FILE if dest else None


def _token_key(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _cached_usage(dest, token, max_age_s, now, selected_model):
    """Re-parse a recent successful usage reading for this exact token, or None."""
    path = _usage_cache_path(dest)
    if path is None:
        return None
    try:
        entry = _json_bytes(private_fs.read_bytes(str(path), MAX_BODY + 4096, private=True))
        age = _clock() - float(entry['at'])
        if entry.get('token') != _token_key(token) or not 0 <= age <= max_age_s or not isinstance(entry.get('usage'), dict):
            return None
        # Parsed against the current time: a reset that has since passed fails closed.
        return _parse_usage_payload(entry['usage'], now, selected_model)
    except Exception:
        return None


def _store_usage(dest, token, data):
    path = _usage_cache_path(dest)
    if path is None:
        return
    try:
        raw = json.dumps({'at': _clock(), 'token': _token_key(token), 'usage': data}).encode('utf-8')
        private_fs.atomic_write(path, raw, MAX_BODY + 4096)
    except Exception:
        pass


def _backoff_path(dest, token):
    # Same managed-account directory is passed by fleet and launcher. Never use
    # HOME here: the launcher deliberately replaces HOME with the engine home.
    return Path(dest) / ('claude-usage-backoff-' + _token_key(token) + '.json') if dest else None


def _read_backoff(dest, token):
    path = _backoff_path(dest, token)
    try:
        entry = _json_bytes(private_fs.read_bytes(str(path), 4096, private=True))
        at, until = float(entry['at']), float(entry['until'])
        failures = entry['failures']
        if (entry.get('token') != _token_key(token) or not math.isfinite(at)
                or not math.isfinite(until) or at > _clock() or until < at
                or isinstance(failures, bool) or not isinstance(failures, int)
                or not 0 <= failures <= 32):
            return None
        return entry
    except Exception:
        return None


def _write_backoff(dest, token, entry):
    path = _backoff_path(dest, token)
    if path is not None:
        private_fs.atomic_write(path, json.dumps(entry).encode(), 4096)


def _record_rate_limit(dest, token, headers):
    prior = _read_backoff(dest, token) or {}
    failures = min(32, prior.get('failures', 0) + 1)
    at = _clock()
    delay = min(3600, 120 * 2 ** (failures - 1))
    retry = headers.get('Retry-After') if headers else None
    if retry is not None:
        try:
            seconds = float(retry)
        except (ValueError, TypeError):
            try:
                stamp = parsedate_to_datetime(str(retry))
                seconds = stamp.timestamp() - at if stamp.tzinfo else 0
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        if math.isfinite(seconds):
            delay = max(delay, seconds)
    _write_backoff(dest, token, {'token': _token_key(token), 'at': at,
                               'until': at + delay, 'failures': failures})


def _rate_limited_result(dest, token, now, selected_model):
    cached = _cached_usage(dest, token, USAGE_RATE_LIMITED_REUSE_S, now, selected_model)
    if cached is not None and all(w['percent_remaining'] > 0 for w in cached['claude']['windows'].values()):
        return cached
    return {'claude': {'status': 'unknown', 'error': 'native_claude_rate_limited',
                       'details': {'limit_reached': True, 'managed': True}, 'windows': {}}}


def _probe(bundle, dest, opener=None, now=None, model=None, requested_model=None):
    """Probe authoritative managed Claude quota for a portable token bundle."""
    now = now or dt.datetime.now(dt.timezone.utc)
    selected_model = _extract_selected_model(bundle, requested_model or model)
    try:
        token = _extract_token(bundle, dest)
    except ProbeError as exc:
        return {
            'claude': {
                'status': 'unknown',
                'error': str(exc),
                'details': {'limit_reached': None, 'managed': True},
                'windows': {},
            }
        }

    opener = opener or urllib.request.build_opener(NoRedirect)

    # 1. Primary path: GET /api/oauth/usage
    req_usage = urllib.request.Request(
        f'{USAGE_URL}?cedar_ember=1&skip_spend=1',
        headers={
            'Authorization': f'Bearer {token}',
            'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'Claude-Code/2.1.198',
        },
        method='GET',
    )
    backoff = _read_backoff(dest, token)
    if backoff and backoff['until'] > _clock():
        return _rate_limited_result(dest, token, now, selected_model)
    cached = _cached_usage(dest, token, USAGE_REUSE_S, now, selected_model)
    if cached is not None:
        return cached
    try:
        with opener.open(req_usage, timeout=15) as resp:
            data = _json_bytes(resp.read(MAX_BODY + 1))
            result = _parse_usage_payload(data, now, selected_model)
            _store_usage(dest, token, data)
            _write_backoff(dest, token, {'token': _token_key(token), 'at': _clock(),
                                         'until': _clock(), 'failures': 0})
            return result
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {
                'claude': {
                    'status': 'unknown',
                    'error': 'native_claude_auth_failed',
                    'details': {'limit_reached': None, 'managed': True},
                    'windows': {},
                }
            }
        if exc.code == 403:
            # 403 Forbidden: inference-only setup token lacking user:profile scope.
            # Probe count_tokens preflight for native unified subscription headers.
            pass
        elif exc.code == 429:
            try:
                _record_rate_limit(dest, token, exc.headers)
            except Exception:
                # A storage refusal must never turn a provider refusal into admission.
                return {'claude': {'status': 'unknown', 'error': 'native_claude_rate_limited',
                                   'details': {'limit_reached': True, 'managed': True}, 'windows': {}}}
            return _rate_limited_result(dest, token, now, selected_model)
        else:
            return {
                'claude': {
                    'status': 'unknown',
                    'error': f'native_claude_http_{exc.code}',
                    'details': {'limit_reached': None, 'managed': True},
                    'windows': {},
                }
            }
    except ProbeError as exc:
        return {
            'claude': {
                'status': 'unknown',
                'error': str(exc),
                'details': {'limit_reached': None, 'managed': True},
                'windows': {},
            }
        }
    except Exception:
        return {
            'claude': {
                'status': 'unknown',
                'error': 'native_claude_probe_failed',
                'details': {'limit_reached': None, 'managed': True},
                'windows': {},
            }
        }

    # 2. Secondary path: count_tokens preflight for native unified rate-limit headers
    api_model = resolve_api_model(selected_model)
    payload = json.dumps({
        'model': api_model,
        'messages': [{'role': 'user', 'content': 'quota-probe'}],
    }).encode('utf-8')
    req_ct = urllib.request.Request(
        COUNT_TOKENS_URL,
        data=payload,
        headers={
            'Authorization': f'Bearer {token}',
            'anthropic-beta': 'oauth-2025-04-20',
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
            'User-Agent': 'Claude-Code/2.1.198',
        },
        method='POST',
    )
    try:
        with opener.open(req_ct, timeout=15) as resp:
            headers = dict(resp.headers.items())
            try:
                return _parse_unified_headers(headers, now, selected_model)
            except ProbeError:
                # count_tokens returns token RPM/TPM limits, not subscription unified headers.
                # Inference-only token cannot provide authoritative subscription usage.
                return {
                    'claude': {
                        'status': 'unknown',
                        'error': 'native_claude_scope_inference_only',
                        'details': {'limit_reached': None, 'managed': True},
                        'windows': {},
                    }
                }
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            err = 'native_claude_auth_failed'
        elif exc.code == 403:
            err = 'native_claude_scope_inference_only'
        elif exc.code == 429:
            try:
                _record_rate_limit(dest, token, exc.headers)
            except Exception:
                pass
            return _rate_limited_result(dest, token, now, selected_model)
        else:
            err = f'native_claude_http_{exc.code}'
        return {
            'claude': {
                'status': 'unknown',
                'error': err,
                'details': {'limit_reached': True if exc.code == 429 else None, 'managed': True},
                'windows': {},
            }
        }
    except ProbeError as exc:
        return {
            'claude': {
                'status': 'unknown',
                'error': str(exc),
                'details': {'limit_reached': None, 'managed': True},
                'windows': {},
            }
        }
    except Exception:
        return {
            'claude': {
                'status': 'unknown',
                'error': 'native_claude_probe_failed',
                'details': {'limit_reached': None, 'managed': True},
                'windows': {},
            }
        }


def probe(bundle, dest, opener=None, now=None, model=None, requested_model=None):
    """Serialize reads for an exact token across fleet and launcher processes.

    A contender never waits on a network request or starts another one. It may
    use only the same bounded safe fallback as a provider rate-limit refusal.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    selected_model = _extract_selected_model(bundle, requested_model or model)
    try:
        token = _extract_token(bundle, dest)
    except ProbeError:
        return _probe(bundle, dest, opener, now, model, requested_model)
    if not dest:
        return _probe(bundle, dest, opener, now, model, requested_model)
    lock = Path(dest) / ('claude-usage-' + _token_key(token) + '.lock')
    try:
        with private_fs.exclusive_lock(lock):
            return _probe(bundle, dest, opener, now, model, requested_model)
    except OSError:
        return _rate_limited_result(dest, token, now, selected_model)
