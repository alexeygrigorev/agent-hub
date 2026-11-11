"""Explicit native-to-central Claude import. Never writes the native login."""
import json
import os
from pathlib import Path
import time

from . import bus, config, native_import_host, private_fs, provisioning

AUTH_PATH = '.claude/.credentials.json'


def collect():
    directory = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser().absolute()
    # A Hub-managed home is a distribution target, never a native login source.
    if any(part in ('managed-engines', 'managed_engines') for part in directory.parts):
        raise ValueError('Native Claude home required')
    try:
        # Native Claude does not make its config directory private. Pin every
        # directory without following symlinks; the credential itself must be
        # owned, private, regular, singly linked and bounded.
        target = directory / '.credentials.json'
        with private_fs.parent(target, private=False) as anchor:
            fd = private_fs._open(anchor, target, os.O_RDONLY, private=True)
            with os.fdopen(fd, 'rb') as stream:
                raw = stream.read(16001)
        if len(raw) > 16000:
            raise ValueError()
        value = json.loads(raw)
        oauth = value['claudeAiOauth']
        if not isinstance(oauth, dict):
            raise ValueError()
        if not all(isinstance(oauth.get(key), str) and oauth[key].strip()
                   for key in ('accessToken', 'refreshToken')):
            raise ValueError()
        expiry = oauth.get('expiresAt')
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not time.time() * 1000 < expiry < 1e15:
            raise ValueError()
        # Do not import unrelated MCP OAuth credentials, cookies or settings.
        fields = ('accessToken', 'refreshToken', 'expiresAt', 'refreshTokenExpiresAt',
                  'scopes', 'subscriptionType', 'subscriptionTypeSource', 'rateLimitTier')
        clean = {key: oauth[key] for key in fields if key in oauth}
        return {AUTH_PATH: json.dumps({'claudeAiOauth': clean})}
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('Native Claude credential unavailable, unsafe or expired') from None


def command(args):
    if args.provider != 'claude' or args.label != 'hub-claude':
        raise ValueError('Unsupported native import')
    operator = config.load_config()
    base = os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL') or operator.get('base_url')
    machine, host_request = native_import_host.bind(base)
    listing = bus._do_request('GET', '/api/engines')
    rows = [row for row in listing.get('credentials', []) if row.get('label') == args.label]
    if len(rows) != 1 or type(rows[0].get('generation')) is not int or rows[0]['generation'] < 0:
        raise ValueError('Central credential generation unavailable')
    generation = rows[0]['generation']
    if machine not in rows[0].get('hosts', []):
        raise ValueError('This host is not approved for the managed Claude account')
    files = collect()
    saved = bus._do_request('POST', '/api/engines/import-native', {
        'provider': 'claude', 'label': args.label, 'expected_generation': generation, 'files': files,
    })
    published = saved.get('credential_generation')
    if type(published) is not int or published != generation + 1:
        raise ValueError('Import receipt did not confirm the next generation; no sync attempted')

    def current_only(method, path, payload=None):
        response = host_request(method, path, payload)
        if method == 'GET' and path == '/api/engines/sync':
            bundles = response.get('engines')
            if (not isinstance(bundles, list) or len(bundles) != 1
                    or bundles[0].get('label') != args.label
                    or type(bundles[0].get('credential_generation')) is not int
                    or bundles[0]['credential_generation'] < published):
                raise ValueError('Managed sync generation is older than the native import')
            # This command must never complete an unrelated pending login.
            response = dict(response, logins=[])
        return response

    try:
        provisioning.sync(request=current_only)
        verified = bus._do_request('GET', '/api/engines')
        states = [account for row in verified.get('sync_status', [])
                  if row.get('machine_id') == machine
                  for account in row.get('accounts', []) if account.get('label') == args.label]
        if (len(states) != 1 or type(states[0].get('acked_generation')) is not int
                or states[0]['acked_generation'] < published
                or states[0]['acked_generation'] != states[0].get('credential_generation')):
            raise ValueError()
    except Exception:
        raise ValueError('Native import saved at generation %d; managed sync or acknowledgement is pending. Check hub engines list and verify managed Opus health before retrying import' % published) from None
    return {'status': 'imported', 'provider': 'claude', 'label': args.label,
            'credential_generation': published, 'machine_id': machine,
            'acked_generation': states[0]['acked_generation']}
