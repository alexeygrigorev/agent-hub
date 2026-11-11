"""Read-only enrolled host binding for native credential import."""
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

from . import api, config

_ENROLLMENT_ERROR = 'Native import requires HTTPS and an enrolled host on the same Hub'


def _https_origin(base):
    config.canonical_origin(base)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'https' or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError(_ENROLLMENT_ERROR)


def bind(operator_base):
    """Pin host identity and transport separately from the operator profile.

    HUB2_CONFIG is a per-worker selector, never host enrollment authority.
    An existing current enrollment must pass validation; it never falls back
    to a different identity after a failed check.
    """
    try:
        _https_origin(operator_base)
        current = Path.home() / '.config/agent-hub/v2.json'
        explicit = bool(os.environ.get('HUB_CONFIG_FILE'))
        # Check ancestor links too: a dangling linked directory must never
        # turn a failed current enrollment into legacy fallback authority.
        current_selected = not explicit and (config._has_symlink(current) or current.exists())
        path = current if current_selected else config.get_host_config_path()
        host = config._load_from(path)  # bounded, owned, private, no-follow
        is_v2 = current_selected or 'server' in host or 'token' in host
        base = host.get('server' if is_v2 else 'base_url')
        token = host.get('token' if is_v2 else 'access_token')
        machine = host.get('machine_id')
        _https_origin(base)
        if (host.get('client_kind') not in (None, 'host', 'machine')
                or any(key in host for key in ('worker', 'worker_launch_id'))
                or config._is_default_human_config_path(path)
                or not config.is_valid_machine_id(machine)
                or not isinstance(token, str) or not token
                or any(not 33 <= ord(c) <= 126 for c in token)
                or not config.same_origin(operator_base, base)):
            raise ValueError(_ENROLLMENT_ERROR)
    except (OSError, ValueError, TypeError):
        raise ValueError(_ENROLLMENT_ERROR) from None
    base = base.rstrip('/')

    def request(method, path, payload=None):
        if (method, path) not in (
                ('GET', '/api/engines/sync'),
                ('POST', '/api/engines/hub-claude'),
                ('POST', '/api/engines/sync-report')):
            raise ValueError('Native import host engine API path required')
        req = urllib.request.Request(base + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        return api.safe_http_json(req)

    return machine, request
