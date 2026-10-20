"""Client transport for native session operations; CLI-only protocols return None.

Explicit executable paths remain an operator-controlled binary override. No
fallback on Client failure: effects may have happened and must be reconciled.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess


def client_call(executable, args, *, env=None):
    """Return a CompletedProcess-shaped response, or None for CLI-only calls."""
    if executable not in ('a', 'aplexer') or not args:
        return None
    operation, *tokens = args
    supported = {
        'list': {'--all', '--json'},
        'status': {'--workspace', '--tag', '--json'},
        'capture': {'--workspace', '--tag', '--bytes'},
        'kill': {'--workspace', '--tag', '--signal', '--grace-ms', '--json'},
        'start': {'--workspace', '--tag', '--engine', '--profile', '--cwd',
                  '--memory', '--pids', '--json'},
    }
    if operation not in supported:
        return None
    values, positional, command = {}, [], []
    flags = {'--all', '--json'}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == '--' and operation == 'start':
            command = tokens[i + 1:]
            break
        if token.startswith('-'):
            if token not in supported[operation]:
                return None
            if token in flags:
                values[token] = True
            else:
                i += 1
                if i >= len(tokens):
                    return None
                values[token] = tokens[i]
        else:
            positional.append(token)
        i += 1
    if len(positional) > 1 or (positional and '--tag' in values):
        return None
    # Lazy import keeps read-only HTTP CLI / portable zipapp usable without
    # the native extension. Native session commands require the uv dependency.
    try:
        from aplexer import Client, AplexerError
    except ModuleNotFoundError as exc:
        import zipimport
        if exc.name == 'aplexer' and isinstance(__loader__, zipimport.zipimporter):
            # The self-contained HTTP/host bootstrap zipapp cannot carry a
            # platform-specific extension. Preserve its supported CLI route.
            return None
        raise
    environment = os.environ if env is None else env
    client = Client(state_dir=environment.get('APLEXER_STATE_DIR'),
                    runtime_dir=environment.get('APLEXER_RUNTIME_DIR'),
                    config=environment.get('APLEXER_CONFIG'))
    try:
        if operation == 'list':
            result = client.snapshot(running=not values.get('--all', False))
        elif operation == 'start':
            kwargs = {key[2:].replace('-', '_'): value for key, value in values.items()
                      if key != '--json'}
            if 'pids' in kwargs:
                kwargs['pids'] = int(kwargs['pids'])
            result = client.start(command=command, **kwargs).raw
        else:
            selector = values.get('--tag') or (positional[0] if positional else None)
            if selector is None:
                return None
            workspace = values.get('--workspace')
            if workspace:
                # Resolve scoped tag to UUID; never let an ambient workspace
                # choose another session with the same tag.
                rows = [r for r in client.snapshot() if r.get('tag') == selector
                        and os.path.realpath(r.get('workspace', '')) == os.path.realpath(workspace)]
                if len(rows) != 1:
                    return subprocess.CompletedProcess([executable, *args], 1, '', 'Native target ambiguous or absent')
                selector = rows[0]['id']
            if operation == 'status':
                result = client.status(selector).raw
            elif operation == 'capture':
                output = client.capture(selector, max_bytes=int(values.get('--bytes', 65536)))
                return subprocess.CompletedProcess([executable, *args], 0, output.decode('utf-8', 'replace'), '')
            else:
                name = values.get('--signal', 'TERM')
                number = int(name) if name.isdigit() else int(getattr(signal, 'SIG' + name.removeprefix('SIG')))
                client.kill(selector, signal=number, grace_ms=int(values.get('--grace-ms', 2000)))
                result = {}
        return subprocess.CompletedProcess([executable, *args], 0, json.dumps(result), '')
    except AplexerError:
        # Do not leak native errors, paths, captured text or environment.
        return subprocess.CompletedProcess([executable, *args], 1, '', 'Native client operation failed; reconcile effects')
