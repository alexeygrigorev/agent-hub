"""Managed engine bundle migration and output privacy acceptance tests."""
import json
import os
import re
import stat
from pathlib import Path

import pytest

from agent_hub_host import provisioning as p
from agent_hub_host.command_diagnostics import output_tail
from agent_hub_host.log_redaction import redact_log
from agent_hub_host import bootstrap_pins


def test_log_and_cli_diagnostics_redact_token_patterns():
    jwt = 'eyJ' + 'a' * 24 + '.' + 'b' * 20 + '.' + 'c' * 20
    opaque = 'opaque-' + 'x' * 48
    vendor_key = 'sk-' + 'k' * 28
    samples = [
        jwt,
        jwt[:28],
        json.dumps({'accessToken': jwt, 'refresh_token': opaque,
                    'id_token': opaque, 'clientSecret': opaque}),
        'Authorization: Bearer ' + opaque,
        'access_token=' + opaque,
        vendor_key,
    ]
    logged = '\n'.join(redact_log(value) for value in samples)
    cli = '\n'.join(output_tail(value) for value in samples)
    patterns = (
        re.compile(r'eyJ[A-Za-z0-9_-]{5,}'),
        re.compile(r'\bsk-[A-Za-z0-9_-]{8,}'),
        re.compile(r'(?i)\bBearer\s+(?!\[redacted\])\S+'),
        re.compile(r'(?i)(?:access|refresh|id|auth|oauth)[_-]?token\s*[:=]\s*(?!\[redacted\])[^,\s}\"]+'),
        re.compile(r'(?i)client[_-]?secret\s*[:=]\s*(?!\[redacted\])[^,\s}\"]+'),
    )
    if any(pattern.search(value) for value in (logged, cli) for pattern in patterns):
        pytest.fail('sensitive output remained after redaction')
    if any(fragment in value for value in (logged, cli)
           for fragment in (jwt, jwt[:28], opaque, vendor_key)):
        pytest.fail('sensitive output remained after redaction')
    assert '[redacted]' in logged.lower()
    assert '[redacted]' in cli.lower()


def _codex_bundle(version, *, label='security-test', revision='11111111-1111-4111-8111-111111111111'):
    return {
        'label': label,
        'provider': 'codex',
        'version': version,
        'revision': revision,
        'args': [],
        'files': {'.codex/auth.json': '{"signed_in":true}'},
        'env': {},
    }


@pytest.fixture
def managed_engine(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'managed'))
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer/config.toml'))
    monkeypatch.setattr(p, 'load_git_identity', lambda: {})
    return p.root()


def test_sync_uses_shipped_codex_pin_after_account_bundle_bump(managed_engine, monkeypatch):
    pin = '@openai/codex@0.170.7'
    monkeypatch.setitem(bootstrap_pins.ENGINE_PACKAGES, 'codex', pin)
    managed_engine.mkdir(parents=True, exist_ok=True)
    # pathlib mkdir leaves 0755; the private fs admits owner-only dirs alone.
    managed_engine.chmod(0o700)
    npm_installs = []

    def fake_npm(argv, **kwargs):
        package = argv[-1]
        npm_installs.append(package)
        engine = Path(argv[argv.index('--prefix') + 1])
        binary = engine / 'node_modules/.bin/codex'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)

    monkeypatch.setattr(p.subprocess, 'run', fake_npm)
    monkeypatch.setattr(p, 'profiles', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(p, 'sync_native_antigravity', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(p, 'sync_opencode_device', lambda *_args, **_kwargs: None)
    from agent_hub_host import engine_login
    monkeypatch.setattr(engine_login, 'cancel_removed', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engine_login, 'tick', lambda *_args, **_kwargs: None)

    stale = _codex_bundle('0.162.1')

    def request(method, path, body=None):
        if method == 'GET' and path == '/api/engines/sync':
            return {'engines': [dict(stale, files=dict(stale['files']), env={})]}
        return {}

    p.sync(request)
    p.sync(request)

    dest = managed_engine / (stale['label'] + '-' + stale['revision'])
    expected_version = pin.rsplit('@', 1)[1]
    installed = p.load_bundle(dest)
    if installed.get('version') != expected_version:
        pytest.fail('sync did not retain the shipped Codex version')
    if npm_installs != [pin]:
        pytest.fail('sync installed a version outside the shipped Codex pin')


def test_legacy_receipt_migrates_without_replacing_live_codex_auth(managed_engine):
    version = bootstrap_pins.ENGINE_PACKAGES['codex'].rsplit('@', 1)[1]
    bundle = _codex_bundle(version)
    dest = managed_engine / (bundle['label'] + '-' + bundle['revision'])
    p.write_json(dest / 'bundle.json', bundle)

    auth_path = dest / 'home/.codex/auth.json'
    existing_auth = b'{"signed_in":"current-session"}'
    p.private_fs.atomic_write(auth_path, existing_auth, 32768)
    executable = dest / 'engine/node_modules/.bin/codex'
    with p.private_fs.parent(executable, create=True):
        pass
    p.private_fs.atomic_write(executable, b'codex test executable', 32768)

    p.install(bundle)

    receipt = p.read_json(dest / 'bundle.json')
    if (receipt.get('files') != {} or receipt.get('env') != {}
            or receipt.get('credentials_file') != 'credentials.json'):
        pytest.fail('bundle receipt still carries credential content or lacks its fixed reference')
    receipt_bytes = (dest / 'bundle.json').read_bytes()
    if b'current-session' in receipt_bytes or b'signed_in' in receipt_bytes:
        pytest.fail('bundle receipt contains auth-file content')

    credential_path = dest / 'credentials.json'
    credential_stat = credential_path.stat()
    if stat.S_IMODE(credential_stat.st_mode) != 0o600:
        pytest.fail('credential file permissions are not owner-only')
    if hasattr(os, 'getuid') and credential_stat.st_uid != os.getuid():
        pytest.fail('credential file is not owned by the current user')
    saved = p.read_json(credential_path)
    if saved.get('files') != bundle['files'] or saved.get('env') != bundle['env']:
        pytest.fail('migration did not preserve the imported credentials')
    if auth_path.read_bytes() != existing_auth:
        pytest.fail('migration replaced the existing session auth file')
    if p.load_bundle(dest) != bundle:
        pytest.fail('migrated bundle did not resolve through its credential reference')
