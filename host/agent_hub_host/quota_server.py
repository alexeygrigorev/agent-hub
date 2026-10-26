"""Hub backend quota polling, independent of enrolled machines and host reports.

Run on the Hub's credential-bearing server every five minutes. Only sanitized
account observations cross the wire. Provider credentials remain server-local.
"""
import argparse
import datetime
import http.cookiejar
import json
import re
from pathlib import Path
import stat
import urllib.error
import urllib.parse
import urllib.request

from . import config, quota_snapshot


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def collect_accounts(settings, *, runner=None):
    """Probe every configured account; an absent/failed probe replaces old success."""
    accounts = settings.get('accounts')
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 32:
        raise ValueError('Expected 1..32 account definitions')
    seen = set()
    for account in accounts:
        if (not isinstance(account, dict) or set(account) != {'account', 'provider'}
                or not isinstance(account['account'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', account['account'])
                or account['provider'] not in quota_snapshot.ALLOWED_PROVIDERS
                or account['account'] in seen):
            raise ValueError('Invalid account definition')
        seen.add(account['account'])
    raw = quota_snapshot.execute_quse(settings.get('quse', 'quse'), runner=runner)
    snapshot = quota_snapshot.create_quota_snapshot(raw, 'hub-server')
    results = []
    for account in accounts:
        provider = account['provider']
        quota = snapshot['routes'].get(provider)
        if quota is None:
            quota = quota_snapshot.sanitize_provider(provider, None, datetime.datetime.now(datetime.timezone.utc))
        results.append({**account, 'quota': quota})
    # Managed accounts are added once in Hub and installed on the backend by
    # provisioning. Discover their safe label/provider catalog automatically.
    state_path = Path(settings.get('managed_state_file', config.get_host_config_path().parent / 'managed-engines/state.json'))
    if state_path.exists():
        state = json.loads(state_path.read_text())
        entries = state
        if not isinstance(entries, list) or len(entries) > 32:
            raise ValueError('Invalid managed account catalog')
        argv = settings.get('managed_quota_argv', ['hub', 'engines', 'quota'])
        try:
            managed = quota_snapshot.execute_quse(argv, runner=runner).get('accounts', [])
        except Exception:
            managed = []
        if not isinstance(managed, list):
            managed = []
        now = datetime.datetime.now(datetime.timezone.utc)
        # Merge managed observations by canonical account: dispatch executes
        # the managed HOME credential, so a managed observation must replace
        # the personal server-local probe for the same fleet account
        # (wrong-account-key fix for intake f6709905: bare-label collision
        # hid a managed 401 behind a fresh personal token, routing /goal
        # starts at a dead account while `hub quota` looked fresh). The
        # server dedupes snapshots on canonical account alone
        # (src/quota.ts putServerSnapshot: duplicate_account 400), so the
        # payload must carry unique accounts: managed (codex, codex-orin)
        # replaces personal (codex-orin, codex-orin) in place instead of
        # appending a second `codex-orin` row. Canonicalization mirrors the
        # server (codex-orin provider -> codex; urgent-muse account ->
        # shared-go); every row stays sanitized, no raw material crosses.
        def _canonical_provider(p):
            return 'codex' if p == 'codex-orin' else p
        def _canonical_account(a):
            return 'shared-go' if a == 'urgent-muse' else a
        by_account = {}
        for idx, row in enumerate(results):
            if isinstance(row, dict) and isinstance(row.get('account'), str):
                by_account.setdefault(_canonical_account(row['account']), idx)
        managed_seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('Invalid managed account catalog')
            label, provider = entry.get('label'), entry.get('provider')
            if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', label) or provider not in quota_snapshot.ALLOWED_PROVIDERS:
                raise ValueError('Invalid managed account catalog')
            key = _canonical_account(label)
            if key in managed_seen:
                raise ValueError('Invalid managed account catalog')
            managed_seen.add(key)
            row = next((r for r in managed if isinstance(r, dict) and r.get('account') == label and r.get('provider') == provider), {})
            quota = quota_snapshot.sanitize_provider(provider, row.get('quota'), now)
            if key in by_account:
                results[by_account[key]] = {'account': label, 'provider': provider, 'quota': quota}
                continue
            if len(results) >= 32:
                raise ValueError('Too many account observations')
            seen.add(label)
            by_account[key] = len(results)
            results.append({'account': label, 'provider': provider, 'quota': quota})
    return {'captured_at': quota_snapshot.format_iso_utc(datetime.datetime.now(datetime.timezone.utc)), 'accounts': results}


def admin_opener(base_url, token_file):
    """Use the existing private bootstrap token; never follow credential redirects."""
    origin = config.canonical_origin(base_url)
    url = urllib.parse.urlsplit(base_url)
    if origin[0] != 'https' or url.path not in ('', '/') or url.query or url.fragment:
        raise ValueError('Expected HTTPS origin')
    source = Path(token_file)
    if source.is_symlink() or stat.S_IMODE(source.stat().st_mode) & 0o077:
        raise ValueError('Admin token must be a private regular file')
    token = source.read_text().strip()
    if not 16 <= len(token) <= 2048:
        raise ValueError('Invalid admin token file')
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    # Cloudflare's existing browser-integrity rule rejects Python's default UA.
    opener.addheaders = [('User-Agent', 'Mozilla/5.0 (AgentHub quota server)')]
    request = urllib.request.Request(base_url + '/auth/token',
        data=urllib.parse.urlencode({'token': token}).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        opener.open(request, timeout=20).close()
        raise ValueError('Unexpected sign-in response')
    except urllib.error.HTTPError as error:
        error.close()
        if error.code != 303:
            raise ValueError('Server authentication failed') from None
    with opener.open(base_url + '/auth/me', timeout=20) as response:
        me = json.loads(response.read(32768))
    if me.get('authenticated') is not True or not isinstance(me.get('csrf_token'), str):
        raise ValueError('Admin session unavailable')
    return opener, me['csrf_token']


def publish(settings, payload):
    base = settings['base_url'].rstrip('/')
    opener, csrf = admin_opener(base, settings['admin_token_file'])
    data = json.dumps(payload).encode()
    if len(data) > quota_snapshot.MAX_SNAPSHOT_BYTES:
        raise ValueError('Account snapshot too large')
    request = urllib.request.Request(base + '/api/quota/server', data=data,
        headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, method='POST')
    with opener.open(request, timeout=20) as response:
        receipt = json.loads(response.read(4096))
    if receipt.get('status') != 'stored' or receipt.get('accounts') != len(payload['accounts']):
        raise ValueError('Invalid account receipt')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        settings = json.loads(Path(args.config).read_text())
        payload = collect_accounts(settings)
        publish(settings, payload)
        print('Hub server quota: stored %d account observations.' % len(payload['accounts']))
        return 0
    except Exception:
        # Provider errors/HTTP bodies/config paths may contain private material.
        print('Hub server quota collection failed; inspect configuration and provider authentication.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
