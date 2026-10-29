"""AWS Gate credential-process for Hub-managed workers (stdlib only).

Reads the per-machine Gate env (URL + bearer, host-only secret, never in the
repo) WITHOUT trusting HOME (managed workers isolate HOME), then performs the
Gate HTTP call and prints the standard credential_process JSON.
"""
from __future__ import annotations

import json
import os
import shlex
import urllib.error
import urllib.request
from pathlib import Path


def parse_env_file(path: Path) -> dict:
    values: dict = {}
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith('unset '):
            continue
        if line.startswith('export '):
            try:
                parts = shlex.split(line)
            except Exception:
                continue
            assignments = parts[1:]
        else:
            assignments = [line]
        for item in assignments:
            if '=' not in item:
                continue
            key, value = item.split('=', 1)
            values[key] = value
    return values


def default_gate_env_file() -> Path | None:
    """Locate the host Gate env without trusting HOME.

    Order: AWS_CONFIG_FILE-derived real base (<base>/aws-gate/env) when the
    managed env points AWS_CONFIG_FILE at the Hub-owned shared copy; else
    Path.home() only when that home actually holds the Gate env (interactive
    use outside managed isolation).
    """
    cfg = os.environ.get('AWS_CONFIG_FILE')
    if cfg:
        try:
            p = Path(cfg).expanduser()
            parts = p.parts
            # <base>/agent-hub/shared/aws/config (POSIX and Windows)
            if len(parts) >= 4 and parts[-4:] == ('agent-hub', 'shared', 'aws', 'config') or \
                    (len(parts) >= 4 and parts[-1] == 'config' and parts[-2] == 'aws'
                     and parts[-3] == 'shared' and parts[-4] == 'agent-hub'):
                base = p.parents[3]
                cand = base / 'aws-gate' / 'env'
                return cand
        except Exception:
            pass
    try:
        home_cand = Path.home() / '.config' / 'aws-gate' / 'env'
        if home_cand.is_file():
            return home_cand
    except Exception:
        pass
    return None


def run_credential_process(env_file=None, timeout=10.0) -> int:
    path = Path(env_file).expanduser() if env_file else default_gate_env_file()
    if path is None:
        print('AWS Gate env missing; run the Gate install/pair flow on this host', file=__import__('sys').stderr)
        return 1
    values = parse_env_file(path)
    url = values.get('AWS_CONTAINER_CREDENTIALS_FULL_URI')
    token = values.get('AWS_CONTAINER_AUTHORIZATION_TOKEN')
    if not url or not token:
        print('AWS Gate env missing required entries; re-run Gate install/pair', file=__import__('sys').stderr)
        return 1
    import sys
    request = urllib.request.Request(url, headers={'Authorization': token})
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('AWS Gate is closed or denied: HTTP %s' % getattr(e, 'code', '?'), file=sys.stderr)
        return 1
    except Exception:
        print('AWS Gate credential request failed', file=sys.stderr)
        return 1
    try:
        out = {
            'Version': 1,
            'AccessKeyId': payload['AccessKeyId'],
            'SecretAccessKey': payload['SecretAccessKey'],
            'SessionToken': payload['Token'],
            'Expiration': payload['Expiration'],
        }
    except Exception:
        print('AWS Gate returned an unexpected response', file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


def add_commands(sub):
    parser = sub.add_parser('aws-gate', help='AWS Gate credential provider (no static keys)')
    cmds = parser.add_subparsers(dest='aws_gate_cmd', required=True)
    cp = cmds.add_parser('credential-process', help='Emit credential_process JSON via AWS Gate')
    cp.add_argument('--env-file', default=None)
    cp.add_argument('--timeout', type=float, default=10.0)


def command(args):
    import sys
    if args.aws_gate_cmd == 'credential-process':
        # Credential JSON is already printed to stdout by
        # run_credential_process; return None so the CLI prints nothing else
        # (AWS parses the whole stdout as the credential document).
        rc = run_credential_process(env_file=args.env_file, timeout=args.timeout)
        if rc != 0:
            sys.exit(rc)
        return None
    raise ValueError('unknown aws-gate command')
