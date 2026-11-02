"""Managed claude engine recovery (intake 61bba72f): the Claude Code
auto-updater renamed the shared native binary to .old.<epoch-ms> inside the
managed install and died before replacing it, so every hub launch of claude
on Tabo failed with 'Packaged native engine binary missing' in under a
second. The launcher now disables the session-side updater and restores a
single renamed leftover deterministically.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import provisioning as p


def make_package(dest, cpu='arm64', version='2.1.304'):
    package = dest / 'engine' / 'node_modules' / '@anthropic-ai' / ('claude-code-win32-' + cpu)
    package.mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'version': version}), encoding='utf-8')
    return package


def claude_bundle(version='2.1.304'):
    return {'provider': 'claude', 'version': version}


class EngineBinaryRecoveryTests(unittest.TestCase):
    def arm64(self):
        patcher = patch('platform.machine', return_value='ARM64')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_restores_a_single_renamed_leftover(self):
        self.arm64()
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            package = make_package(dest)
            leftover = package / 'claude.exe.old.1793470149259'
            leftover.write_bytes(b'MZ')
            restored = p.executable_path(claude_bundle(), dest, is_windows=True)
            self.assertEqual(restored, package / 'claude.exe')
            self.assertEqual(restored.read_bytes(), b'MZ')
            self.assertFalse(leftover.exists())

    def test_refuses_ambiguous_leftovers(self):
        self.arm64()
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            package = make_package(dest)
            (package / 'claude.exe.old.111').write_bytes(b'A')
            (package / 'claude.exe.old.222').write_bytes(b'B')
            with self.assertRaisesRegex(ValueError, 'Packaged native engine binary missing'):
                p.executable_path(claude_bundle(), dest, is_windows=True)
            # Ambiguous evidence is never consumed.
            self.assertTrue((package / 'claude.exe.old.111').is_file())
            self.assertTrue((package / 'claude.exe.old.222').is_file())

    def test_missing_binary_still_fails_closed(self):
        self.arm64()
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            make_package(dest)
            with self.assertRaisesRegex(ValueError, 'Packaged native engine binary missing'):
                p.executable_path(claude_bundle(), dest, is_windows=True)

    def test_version_mismatch_is_never_restored(self):
        self.arm64()
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            package = make_package(dest, version='2.1.299')
            (package / 'claude.exe.old.1').write_bytes(b'MZ')
            with self.assertRaisesRegex(ValueError, 'Packaged native engine binary missing'):
                p.executable_path(claude_bundle(), dest, is_windows=True)
            self.assertTrue((package / 'claude.exe.old.1').is_file())

    def test_claude_sessions_never_update_the_managed_install(self):
        with tempfile.TemporaryDirectory() as directory:
            env = p.engine_environment(claude_bundle(), Path(directory) / 'home',
                                       dest=Path(directory))
            self.assertEqual(env['DISABLE_AUTOUPDATER'], '1')

    def test_other_providers_do_not_inherit_the_claude_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            env = p.engine_environment({'provider': 'codex'}, Path(directory) / 'home',
                                       dest=Path(directory))
            self.assertNotIn('DISABLE_AUTOUPDATER', env)


if __name__ == '__main__':
    unittest.main()
