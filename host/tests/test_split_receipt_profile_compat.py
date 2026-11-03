"""A split receipt must not outrun the aplexer profile that still runs an older host release (intake 0e1b61c9)."""
import json
from pathlib import Path

from agent_hub_host import provisioning as p

BUNDLE = {
    'label': 'hub-agy-test', 'provider': 'antigravity', 'version': '1.4.10', 'revision': '22222222-2222-4222-8222-222222222222',
    'args': [], 'files': {p.ANTIGRAVITY_TOKEN: 'token-value'}, 'env': {},
}


def _write_profile(path, release_dir):
    cmd = ['/usr/bin/python3', '-c', "import sys; sys.path.insert(0, %r); " % str(release_dir) + 'from agent_hub_host.provisioning import execute; execute()', BUNDLE['label']]
    path.write_text('version = 1\n\n[profiles.hub-%s]\nengine = "antigravity"\ncommand = %s\n' % (BUNDLE['label'], json.dumps(cmd)), encoding='utf-8')


def _here():
    return Path(p.__file__).resolve().parents[1]


def test_stale_profile_keeps_legacy_receipt_readable_by_the_old_release(tmp_path, monkeypatch):
    profile = tmp_path / 'config.toml'
    _write_profile(profile, tmp_path / 'host-releases-orch' / 'old' / 'host')
    monkeypatch.setenv('APLEXER_CONFIG', str(profile))
    dest = tmp_path / 'managed'
    p.save_bundle(dest, BUNDLE)
    receipt = p.read_json(dest / 'bundle.json')
    # The old release's validate() needs exactly the token in files.
    assert list(receipt['files']) == [p.ANTIGRAVITY_TOKEN]
    assert 'credentials_file' not in receipt
    p.validate(receipt)
    assert p.load_bundle(dest) == BUNDLE


def test_current_profile_or_no_profile_gets_the_split_receipt(tmp_path, monkeypatch):
    profile = tmp_path / 'config.toml'
    monkeypatch.setenv('APLEXER_CONFIG', str(profile))
    dest = tmp_path / 'managed-none'
    p.save_bundle(dest, BUNDLE)  # no profile file at all
    assert p.read_json(dest / 'bundle.json')['files'] == {}
    _write_profile(profile, _here())
    dest = tmp_path / 'managed-current'
    p.save_bundle(dest, BUNDLE)
    receipt = p.read_json(dest / 'bundle.json')
    assert receipt['credentials_file'] == 'credentials.json' and receipt['files'] == {}
    assert p.load_bundle(dest) == BUNDLE
