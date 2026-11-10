#!/usr/bin/env bash
# Execute only the installer's edge block against disposable sentinel sites.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 - "$ROOT" <<'PY'
from pathlib import Path
import os
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
installer = (root / 'deploy/vm/install-hub2-node.sh').read_text()
start = installer.index('if [[ "$HUB_ONLY" != true ]]; then')
end = installer.index('\necho "== state directories"', start)
block = installer[start:end]
transaction = (root / 'deploy/vm/deploy-release-transaction.sh').read_text()
assert '"$TRANSACTION_DIR/Caddyfile.previous"' not in transaction
assert 'install-hub2-node.sh" --hub-only || return 1' in transaction
assert '"$TRANSACTION_DIR/hub.caddy.previous" "$CADDY_SITE"' in transaction
assert 'install -m 644 "$DEPLOY_DIR/Caddyfile"' not in installer

scratch = root / '.local/tmp'
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='hub-site-', dir=scratch) as temporary:
    work = Path(temporary)
    sites = work / 'sites'
    sites.mkdir()
    shared = work / 'Caddyfile'
    original = b'import /etc/caddy/sites/*.caddy\n# infra owned\n'
    shared.write_bytes(original)
    other = sites / 'gate.caddy'
    other.write_bytes(b'gate.example.invalid {\n reverse_proxy localhost:8791\n}\n')
    other_before = other.read_bytes()
    hub = sites / 'hub.caddy'
    hub.write_bytes(b'# old hub site\n')
    binary = work / 'bin'
    binary.mkdir()
    caddy = binary / 'caddy'
    caddy.write_text('#!/bin/sh\n[ "$1" = validate ]\n')
    caddy.chmod(0o755)
    environment = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                       AGENT_HUB_DEPLOY_TEST_MODE='1', AGENT_HUB_TEST_CADDY_CONFIG=str(shared),
                       AGENT_HUB_TEST_CADDY_SITE=str(hub), DEPLOY_DIR=str(root / 'deploy'), HUB_ONLY='false')
    def run():
        return subprocess.run(['bash', '-euc', block], env=environment, capture_output=True)
    assert run().returncode == 0
    assert shared.read_bytes() == original, 'shared config changed'
    assert other.read_bytes() == other_before, 'another site changed'
    assert hub.read_bytes() == (root / 'deploy/hub.caddy').read_bytes()
    shared.write_bytes(b'# missing import\n')
    hub.write_bytes(b'# preserve on preflight refusal\n')
    assert run().returncode != 0
    assert hub.read_bytes() == b'# preserve on preflight refusal\n'
    environment['HUB_ONLY'] = 'true'
    assert run().returncode == 0
    assert hub.read_bytes() == b'# preserve on preflight refusal\n'
    assert other.read_bytes() == other_before
print('Hub Caddy site ownership checks passed.')
PY
