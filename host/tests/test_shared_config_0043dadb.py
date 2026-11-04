"""Shared-config bundle for Hub-launched sessions (intake 0043dadb).

The bundle is versioned and non-secret, synced from the repo into the
Hub-owned shared root; managed sessions get AWS_CONFIG_FILE pointed at that
copy and AWS_SHARED_CREDENTIALS_FILE at a managed-home path that never exists,
so per-machine static keys stay unreachable.
"""
import json
import os
from pathlib import Path

import pytest

from agent_hub_host import provisioning, shared_config


REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_CMD = 'python3 /synthetic/gate/install_aws_gate.py credential-process --env /synthetic/gate/env'


def test_engine_environment_without_bundle_keeps_prior_behavior(tmp_path):
    # Runs before any test syncs the bundle: no synced config means no AWS
    # vars are injected, so hosts that never ran the shared sync are unchanged.
    if shared_config.aws_config_path() is not None and shared_config.aws_config_path().is_file():
        pytest.skip('shared aws config already materialized in this home')
    env = provisioning.engine_environment({'provider': 'codex'}, tmp_path / 'home')
    assert 'AWS_CONFIG_FILE' not in env
    assert 'AWS_SHARED_CREDENTIALS_FILE' not in env


@pytest.fixture
def synced():
    result = shared_config.sync_shared_config(repo_root=REPO_ROOT, settings={'aws_gate_credential_process': GATE_CMD})
    assert result['status'] == 'synced', result
    return result


def test_sync_materializes_the_bundle_into_the_hub_owned_shared_root(synced):
    target = shared_config.aws_config_path()
    assert target is not None and target.is_file()
    text = target.read_text(encoding='utf-8')
    assert '{aws_gate_credential_process}' not in text  # placeholder substituted
    assert '[profile default]' in text and GATE_CMD in text
    assert 'aws_access_key_id' not in text.lower()  # non-secret by design
    config_dir = Path(shared_config.shared_root()) / 'aws' / 'config'
    assert target == config_dir
    state_path = shared_config.shared_root() / shared_config.STATE_FILE
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['version'] == synced['version'] and 'aws' in state['folders']


def test_resync_is_idempotent_and_versions_the_bundle():
    first = shared_config.sync_shared_config(repo_root=REPO_ROOT, settings={'aws_gate_credential_process': GATE_CMD})
    second = shared_config.sync_shared_config(repo_root=REPO_ROOT, settings={'aws_gate_credential_process': GATE_CMD})
    assert first['version'] == second['version']
    assert second['status'] == 'synced'


def test_missing_gate_setting_skips_aws_fail_closed():
    result = shared_config.sync_shared_config(repo_root=REPO_ROOT, settings={})
    assert result['status'] == 'partial'
    folder = next(f for f in result['folders'] if f['folder'] == 'aws')
    assert folder['status'] == 'skipped_setting_missing' and folder['setting'] == 'aws_gate_credential_process'
    assert shared_config.aws_config_path() is None or not shared_config.aws_config_path().is_file()


def test_dry_run_writes_nothing(tmp_path):
    result = shared_config.sync_shared_config(repo_root=REPO_ROOT, settings={'aws_gate_credential_process': GATE_CMD}, dry_run=True)
    assert result['status'] == 'dry_run'
    folder = next(f for f in result['folders'] if f['folder'] == 'aws')
    assert folder['status'] == 'dry_run' and folder['files'] == ['config']
    assert shared_config.aws_config_path() is None or not shared_config.aws_config_path().is_file()


def test_engine_environment_isolates_aws_from_the_machine(synced, tmp_path):
    home = tmp_path / 'managed-home'
    home.mkdir()
    env = provisioning.engine_environment({'provider': 'codex'}, home)
    cfg = shared_config.aws_config_path()
    assert env['AWS_CONFIG_FILE'] == str(cfg)
    # AC4: the shared-credentials path lives inside the managed home and never exists.
    creds = Path(env['AWS_SHARED_CREDENTIALS_FILE'])
    assert creds == shared_config.aws_credentials_path(home) == home / '.aws' / 'credentials'
    assert not creds.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission model')
def test_synced_config_is_group_world_readable_but_never_a_secret(synced):
    mode = shared_config.aws_config_path().stat().st_mode & 0o777
    assert mode == 0o644
