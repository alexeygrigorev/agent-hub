"""Intake 5814e109: the store's Antigravity token reaches the host's native agy path."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agent_hub_host import provisioning as p

BUNDLE = {'label': 'hub-antigravity', 'provider': 'antigravity', 'revision': 'r1',
          'files': {p.ANTIGRAVITY_TOKEN: 'store-token-gen1'}}


class NativeAntigravity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / 'home'
        self.home.mkdir()
        self.managed = base / 'managed'
        self.managed.mkdir(mode=0o700)
        self.patches = [mock.patch.object(Path, 'home', return_value=self.home),
                        mock.patch.object(p, 'root', return_value=self.managed)]
        for patch in self.patches:
            patch.start()
        self.target = self.home / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def bundle(self, token):
        return {**BUNDLE, 'files': {p.ANTIGRAVITY_TOKEN: token}}

    def test_installs_missing_token_privately_and_is_idempotent(self):
        self.assertEqual(p.sync_native_antigravity(BUNDLE), 'installed')
        self.assertEqual(self.target.read_text(), 'store-token-gen1')
        if os.name != 'nt':
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(p.sync_native_antigravity(BUNDLE), 'current')
        self.assertEqual([x.name for x in self.target.parent.iterdir()], ['antigravity-oauth-token'])

    def test_rotation_in_store_replaces_our_own_previous_write(self):
        p.sync_native_antigravity(BUNDLE)
        self.assertEqual(p.sync_native_antigravity(self.bundle('store-token-gen2')), 'installed')
        self.assertEqual(self.target.read_text(), 'store-token-gen2')

    def test_locally_rotated_token_is_never_clobbered(self):
        p.sync_native_antigravity(BUNDLE)
        self.target.write_text('engine-rotated-locally')
        self.assertEqual(p.sync_native_antigravity(self.bundle('store-token-gen2')), 'local_token_kept')
        self.assertEqual(self.target.read_text(), 'engine-rotated-locally')

    def test_preexisting_foreign_token_without_marker_is_kept(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('operator-login')
        self.assertEqual(p.sync_native_antigravity(BUNDLE), 'local_token_kept')
        self.assertEqual(self.target.read_text(), 'operator-login')

    def test_non_antigravity_or_credential_less_bundles_do_nothing(self):
        self.assertEqual(p.sync_native_antigravity({'provider': 'codex', 'files': {}}), 'not_antigravity')
        self.assertEqual(p.sync_native_antigravity({'provider': 'antigravity', 'files': {}}), 'no_store_credential')
        self.assertFalse(self.target.exists())

    def test_windows_style_home_path_uses_the_documented_location(self):
        self.assertEqual(p.native_antigravity_token_path().parts[-3:],
                         ('.gemini', 'antigravity-cli', 'antigravity-oauth-token'))

    def test_token_never_appears_in_marker_or_status(self):
        p.sync_native_antigravity(BUNDLE)
        self.assertNotIn('store-token', (self.managed / 'native-antigravity.json').read_text())


if __name__ == '__main__':
    unittest.main()
