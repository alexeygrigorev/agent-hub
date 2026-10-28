"""Regression guard: the suite must never touch a real hub-worker-auth file.

Incident edb71968: worker_auth.config_path() follows native launch
identity (APLEXER_TAG/APLEXER_WORKSPACE, or HUB_WORKER_CONFIG_FILE),
not HOME/XDG. A suite run from an aplexer session shell therefore wrote
test fixtures (base_url https://hub.example, no worker_launch_id) into
that session's own live credential profile — 20 profiles clobbered,
several live sessions stalled. A disposable HOME alone does not
isolate; the harness (conftest.py) and the runner
(scripts/test-full-local.sh) must clear every native launch selector,
and this test fails if that isolation ever regresses.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host import config
from agent_hub_host import worker_auth

OFFER = '11111111-2222-4333-8444-555555555555'


def real_profile_dir():
    root = Path(__file__).resolve().parents[2]
    if not (root / '.git').exists():
        return None
    return root / '.local' / 'hub-worker-auth'


def snapshot(directory):
    if directory is None or not directory.is_dir():
        return {}
    snap = {}
    for child in directory.iterdir():
        try:
            st = child.stat()
        except OSError:
            continue
        if child.is_file() or child.is_symlink():
            snap[child.name] = (st.st_mtime_ns, st.st_size)
    return snap


class WorkerProfileIsolationTests(unittest.TestCase):
    def test_harness_clears_native_launch_identity(self):
        """The test env carries no live-session identity (harness contract)."""
        for var in ('APLEXER_TAG', 'APLEXER_WORKSPACE', 'APLEXER_SESSION_ID',
                    'HUB_WORKER_CONFIG_FILE'):
            self.assertNotIn(var, os.environ,
                             'harness must clear %s (incident edb71968)' % var)
        leftovers = sorted(v for v in os.environ if v.startswith('APLEXER_'))
        self.assertEqual(leftovers, [],
                         'harness must clear every APLEXER_* selector')
        self.assertIsNone(worker_auth.config_path())

    def test_launch_identity_would_resolve_into_the_real_checkout(self):
        """Documents why clearing matters: with identity set, the repo root
        (not any tmp HOME) is the profile location. Read-only: no write."""
        root = Path(__file__).resolve().parents[2]
        with patch.dict(os.environ, {'APLEXER_TAG': 'task-hub-' + OFFER,
                                     'APLEXER_WORKSPACE': str(root)}):
            self.assertEqual(worker_auth.config_path(),
                             root / '.local' / 'hub-worker-auth' / (OFFER + '.json'))

    def test_save_config_roundtrip_never_reaches_a_real_profile(self):
        """Fixture saves land in the tmp host file; the real dir is untouched."""
        directory = real_profile_dir()
        before = snapshot(directory)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'host.json'
            with patch.object(config, 'get_config_path', return_value=target), \
                 patch('agent_hub_host.worker_auth.config_path', return_value=None):
                config.save_config({'base_url': 'https://hub.example',
                                    'access_token': 'fixture-test-only'})
                self.assertTrue(target.exists())
                self.assertEqual(config.load_config()['access_token'],
                                 'fixture-test-only')
        self.assertEqual(snapshot(directory), before,
                         'save_config touched the real hub-worker-auth dir')

    def test_provision_writes_only_under_the_given_tmp_workspace(self):
        """worker_auth.provision writes exactly one file under tmp; real dir untouched."""
        directory = real_profile_dir()
        before = snapshot(directory)
        request = Mock(return_value=dict(
            worker_launch_id=OFFER, client_kind='agent',
            access_token='hubt_fake-test-only', refresh_token='hubr_fake-test-only',
            base_url='https://hub.test', worker_expires_at=9999999999999))
        with tempfile.TemporaryDirectory() as tmp:
            path = worker_auth.provision(tmp, OFFER, request)
            self.assertTrue(str(path).startswith(tmp))
            self.assertEqual(path.parent.name, 'hub-worker-auth')
        self.assertEqual(snapshot(directory), before,
                         'provision touched the real hub-worker-auth dir')


if __name__ == '__main__':
    unittest.main()
