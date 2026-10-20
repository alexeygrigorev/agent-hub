"""Read native Google Code Assist quota, never Antigravity or model inference.

Protocol: @google/gemini-cli 0.64.0 core code_assist/server.ts and oauth2.ts.
Only existing-account discovery, retrieveUserQuota, and OAuth refresh are used.
No onboarding, project creation, credit activation, or generateContent requests.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from . import private_fs

BASE = 'https://cloudcode-pa.googleapis.com/v1internal:'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
MAX_BODY = 131072
DEFAULT_MODEL = 'gemini-2.5-pro'


class ProbeError(ValueError):
    """Contains a fixed safe code only, never a provider body or credential."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError('native_google_redirect_refused')


def _json_bytes(raw):
    if len(raw) > MAX_BODY:
        raise ProbeError('native_google_response_too_large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProbeError('native_google_invalid_response')
    return value


def _request(opener, url, payload, token=None, form=False):
    headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with opener.open(req, timeout=15) as response:
            if response.geturl() != url:
                raise ProbeError('native_google_redirect_refused')
            return _json_bytes(response.read(MAX_BODY + 1))
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise ProbeError('native_google_http_' + str(code)) from None


def _token(value):
    if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ProbeError('native_google_login_required')
    return value


def _native_client(dest, version):
    """Read the installed vendor's public OAuth application metadata at runtime.

    Application configuration is neither copied into our source nor logged.
    These are the same public desktop-client values the native CLI uses.
    """
    package = dest / 'engine/node_modules/@google/gemini-cli'
    root = dest.resolve()
    metadata = package / 'package.json'
    if not metadata.resolve().is_relative_to(root) or metadata.stat().st_size > MAX_BODY:
        raise ProbeError('native_google_package_mismatch')
    if json.loads(metadata.read_bytes()).get('version') != version:
        raise ProbeError('native_google_package_mismatch')
    total = 0
    for source in sorted((package / 'bundle').glob('*.js'))[:128]:
        if not source.resolve().is_relative_to(root):
            raise ProbeError('native_google_package_mismatch')
        size = source.stat().st_size
        total += size
        if total > 256 * 1024 * 1024:
            break
        if size > 32 * 1024 * 1024:
            continue
        text = source.read_text(encoding='utf-8')
        values = []
        for key in ('OAUTH_CLIENT_ID', 'OAUTH_CLIENT_SECRET'):
            match = re.search(r'\b(?:var|let|const)\s+' + key + r'\s*=\s*([\'\"])([^\'\"\r\n]+)\1', text)
            if not match:
                break
            values.append(match.group(2))
        if len(values) == 2:
            return values
    raise ProbeError('native_google_refresh_metadata_unavailable')


def _refresh(opener, credential, credentials, bundle, dest, now):
    refresh_token = _token(credentials.get('refresh_token'))
    client_id, client_secret = _native_client(dest, bundle.get('version'))
    fresh = _request(opener, TOKEN_URL, {
        'grant_type': 'refresh_token', 'refresh_token': refresh_token,
        'client_id': client_id, 'client_secret': client_secret,
    }, form=True)
    access_token = _token(fresh.get('access_token'))
    seconds = fresh.get('expires_in')
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ProbeError('native_google_invalid_refresh')
    updated = dict(credentials, access_token=access_token,
                   expiry_date=int((now.timestamp() + seconds) * 1000))
    for key in ('refresh_token', 'id_token', 'scope', 'token_type'):
        if key in fresh:
            if key == 'scope' and (not isinstance(fresh[key], str) or len(fresh[key]) > 16384 or '\0' in fresh[key]):
                raise ProbeError('native_google_invalid_refresh')
            updated[key] = _token(fresh[key]) if key != 'scope' else fresh[key]
    private_fs.atomic_write(credential, json.dumps(updated).encode(), MAX_BODY)
    return updated


def _time(value):
    if not isinstance(value, str):
        raise ProbeError('native_google_invalid_quota')
    stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ProbeError('native_google_invalid_quota')
    return stamp.astimezone(dt.timezone.utc)


def probe(bundle, dest, model=None, *, opener=None, now=None):
    """Return a bounded quse-shaped observation for this exact native profile.

    Failures never consult another profile, environment token, AGY, or caches.
    Missing model buckets/windows are unknown, never fabricated full quota.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    dest = Path(dest)
    details = {'source': 'native_google_code_assist', 'observed_at': now.isoformat()}
    try:
        if (bundle.get('provider') != 'gemini' or not dest.is_absolute()
                or not isinstance(bundle.get('version'), str)):
            raise ProbeError('native_google_invalid_profile')
        args = bundle.get('args', [])
        if model is None:
            model = DEFAULT_MODEL
            for flag in ('--model', '-m'):
                if flag in args:
                    index = args.index(flag)
                    if index + 1 >= len(args):
                        raise ProbeError('native_google_invalid_profile')
                    model = args[index + 1]
                    break
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', model):
            raise ProbeError('native_google_invalid_model')
        credential = dest / 'home/.gemini/oauth_creds.json'
        credentials = _json_bytes(private_fs.read_bytes(credential, MAX_BODY))
        token = _token(credentials.get('access_token'))
        opener = opener or urllib.request.build_opener(NoRedirect())
        expiry = credentials.get('expiry_date')
        if expiry is not None:
            if type(expiry) not in (int, float) or not math.isfinite(expiry):
                raise ProbeError('native_google_invalid_credentials')
            if expiry <= now.timestamp() * 1000 + 30000:
                credentials = _refresh(opener, credential, credentials, bundle, dest, now)
                token = _token(credentials.get('access_token'))

        def read_quota(current_token):
            account = _request(opener, BASE + 'loadCodeAssist', {
                'metadata': {'ideType': 'IDE_UNSPECIFIED', 'platform': 'PLATFORM_UNSPECIFIED', 'pluginType': 'GEMINI'},
                'mode': 'HEALTH_CHECK',
            }, current_token)
            if account.get('validationLink'):
                raise ProbeError('native_google_account_validation_required')
            project = account.get('cloudaicompanionProject')
            if isinstance(project, dict):
                project = project.get('id')
            if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,256}', project):
                raise ProbeError('native_google_login_incomplete')
            return _request(opener, BASE + 'retrieveUserQuota', {'project': project}, current_token)

        try:
            quota = read_quota(token)
        except ProbeError as exc:
            if str(exc) != 'native_google_http_401' or not credentials.get('refresh_token'):
                raise
            credentials = _refresh(opener, credential, credentials, bundle, dest, now)
            quota = read_quota(_token(credentials.get('access_token')))
        buckets = quota.get('buckets')
        if not isinstance(buckets, list) or not 0 < len(buckets) <= 128:
            raise ProbeError('native_google_invalid_quota')
        windows = {}
        for index, bucket in enumerate(buckets):
            if not isinstance(bucket, dict) or bucket.get('modelId') != model:
                continue
            if 'CREDIT' in str(bucket.get('tokenType', '')).upper():
                continue
            fraction = bucket.get('remainingFraction')
            if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
                raise ProbeError('native_google_invalid_quota')
            reset = _time(bucket.get('resetTime'))
            if reset <= now:
                raise ProbeError('native_google_stale_quota')
            # The source does not promise a 5h/weekly period. Preserve bucket
            # identity instead of inventing a window duration.
            windows['google_bucket_' + str(index)] = {
                'percent_remaining': fraction * 100, 'reset_at': reset.isoformat(),
            }
            if len(windows) > 32:
                raise ProbeError('native_google_invalid_quota')
        if not windows:
            raise ProbeError('native_google_model_quota_unavailable')
        details.update(limit_reached=any(w['percent_remaining'] <= 0 for w in windows.values()), model=model)
        return {'gemini': {'status': 'ok', 'error': None, 'details': details, 'windows': windows}}
    except ProbeError as exc:
        reason = str(exc)
    except Exception:
        reason = 'native_google_probe_failed'
    return {'gemini': {'status': 'unknown', 'error': reason, 'details': details, 'windows': {}}}
