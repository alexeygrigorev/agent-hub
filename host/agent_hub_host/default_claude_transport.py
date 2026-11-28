"""Passive-only enrolled-host transport for the default-Claude consumer.

The one-way default consumer observes the central ``hub-claude`` login with a
single read: ``GET /api/engines/hub-claude/passive-credential`` (no refresh,
lease, rotation, login or write). The native-import binder deliberately does not
allow that endpoint, and its import / execution / re-pin allowlists must stay
exactly as they are, so this module is a separate binding with ONE allowed
request and no other authority.

Identity and origin checks mirror the managed-execution binding of
``native_import_host`` (current enrolled v2 host only; never the ordinary
profile loader, ``HUB_CONFIG_FILE`` or a worker profile; HTTPS; operator and
enrolled origins equal; valid machine id and header-safe token). Tests compare
the two bindings' refusals over the same fixtures so they cannot drift apart
silently.
"""
import json
import os
from pathlib import Path
import urllib.request

from . import api, config, native_import_host, private_fs, worker_auth

PASSIVE_PATH = '/api/engines/hub-claude/passive-credential'
ERROR = 'Default Claude consumer requires HTTPS and a current enrolled host on the same Hub'


def bind_passive(operator_base):
    """Return ``(machine_id, fetch)``; ``fetch()`` performs the one passive GET.

    Raises ValueError (fixed text) unless the current v2 host enrollment is valid
    and on the same HTTPS origin as ``operator_base``."""
    worker_auth.refuse_v2_profile_io()
    try:
        native_import_host._https_origin(operator_base)
        current = Path.home() / '.config/agent-hub/v2.json'
        # Current enrollment only: an explicit override or any absence is refused,
        # never answered with a legacy or ordinary profile.
        if os.environ.get('HUB_CONFIG_FILE') or config._has_symlink(current):
            raise ValueError(ERROR)
        current.lstat()
        config._forbid_real_user_profile(current)
        host = config._validate_data(json.loads(private_fs.read_bytes(current, 1024 * 1024)))
        base, token, machine = host.get('server'), host.get('token'), host.get('machine_id')
        native_import_host._https_origin(base)
        if (host.get('client_kind') not in (None, 'host', 'machine')
                or any(key in host for key in ('worker', 'worker_launch_id'))
                or config._is_default_human_config_path(current)
                or not config.is_valid_machine_id(machine)
                or not isinstance(token, str) or not token
                or any(not 33 <= ord(c) <= 126 for c in token)
                or not config.same_origin(operator_base, base)):
            raise ValueError(ERROR)
    except (OSError, ValueError, TypeError):
        raise ValueError(ERROR) from None
    base = base.rstrip('/')

    def fetch():
        request = urllib.request.Request(base + PASSIVE_PATH, method='GET',
                                         headers={'Authorization': 'Bearer ' + token,
                                                  'Content-Type': 'application/json'})
        return api.safe_http_json(request)

    return machine, fetch
