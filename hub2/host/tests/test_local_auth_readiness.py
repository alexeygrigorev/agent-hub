"""Synthetic owned-home tests: no real credentials, engines or provider calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from hub2_agent_hub_host.local_auth_readiness import _default_claude_auth_state, local_claude_missing_contexts, FILE_LIMIT, MISSING, UNKNOWN


@unittest.skipUnless(sys.platform == 'linux', 'known-negative backend is deliberately Linux-only')
class LocalAuthReadinessTests(unittest.TestCase):
    def setUp(self):
        # CI's disposable TMPDIR can be below world-writable /tmp, which the
        # production reader correctly refuses. Keep fixture ancestry trusted
        # without relaxing that guard or inspecting real operator files.
        temporary_root = Path(__file__).resolve().parents[3] / '.local/tmp'
        temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='local-auth-readiness-', dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        binary = self.home / 'bin/claude'
        binary.parent.mkdir(mode=0o700)
        binary.write_bytes(b'\x7fELF' + b'\0' * 60)  # Header fixture, never executed.
        binary.chmod(0o700)
        self.env = {'HOME': str(self.home), 'PATH': str(binary.parent)}
        self.credentials = self.home / '.claude/.credentials.json'

    def write(self, path, value, *, raw=False):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(value if raw else json.dumps(value))
        path.chmod(0o600)

    def observe(self, **extra):
        return _default_claude_auth_state({**self.env, **extra}, platform='linux')

    def test_absent_or_empty_oauth_is_negative_and_observation_never_creates_files(self):
        self.assertEqual(self.observe(), MISSING)
        self.assertFalse(self.credentials.parent.exists())
        for value in ({}, {'claudeAiOauth': {}}, {'claudeAiOauth': {'accessToken': '', 'refreshToken': ''}},
                      {'claudeAiOauth': {'accessToken': ' ', 'refreshToken': None, 'expiresAt': 0}}):
            self.write(self.credentials, value)
            before = self.credentials.read_bytes()
            self.assertEqual(self.observe(), MISSING)
            self.assertEqual(self.credentials.read_bytes(), before)

    def test_any_access_or_refresh_material_is_unverified_regardless_of_expiry(self):
        for oauth in ({'accessToken': 'synthetic-access', 'expiresAt': 0},
                      {'refreshToken': 'synthetic-refresh', 'expiresAt': 0},
                      {'accessToken': 'synthetic-access', 'expiresAt': 9999999999999},
                      {'accessToken': '', 'refreshToken': 'synthetic-refresh'}):
            self.write(self.credentials, {'claudeAiOauth': oauth})
            self.assertEqual(self.observe(), UNKNOWN)

    def test_config_location_and_oauth_environment_overrides_stay_unknown(self):
        for key in ('CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FILE',
                    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'ANTHROPIC_AUTH_TOKEN',
                    'ANTHROPIC_API_KEY', 'APLEXER_CONFIG', 'XDG_CONFIG_HOME'):
            self.assertEqual(self.observe(**{key: 'synthetic-override'}), UNKNOWN, key)
        self.assertEqual(self.observe(CLAUDE_FUTURE_AUTH_BACKEND='synthetic-override'), UNKNOWN)
        self.assertEqual(_default_claude_auth_state({'HOME': 'relative', 'PATH': self.env['PATH']}, platform='linux'), UNKNOWN)

    def test_aplexer_default_profile_command_and_engine_environment_are_not_guessed(self):
        path = self.home / '.config/aplexer/config.toml'
        for config in ('default_profile="other"\n',
                       '[engines.claude]\ncommand=["custom-wrapper"]\n',
                       '[engines.claude]\ncommand=["claude","--settings","custom"]\n',
                       '[engines.claude]\ntask_argv=["-p","--settings","custom"]\n',
                       'version=999\n', 'default_profile=""\n',
                       '[engines.claude.env]\nCLAUDE_CONFIG_DIR="custom"\n',
                       '[engines.claude]\nenv_unset=["HOME"]\n', 'not TOML'):
            self.write(path, config, raw=True)
            self.assertEqual(self.observe(), UNKNOWN)
        self.write(path, '[engines.claude]\ncommand=[' + json.dumps(str(self.home/'bin/claude')) + ']\n', raw=True)
        self.assertEqual(self.observe(), MISSING)

    def test_script_wrapper_backend_is_unknown_even_when_named_claude(self):
        (self.home/'bin/claude').write_text('#!/bin/sh\nexit 99\n')
        self.assertEqual(self.observe(), UNKNOWN)

    def test_alternative_global_login_material_or_key_helper_is_unknown(self):
        for path, value in ((self.home/'.claude.json', {'primaryApiKey': 'synthetic-alternate'}),
                            (self.home/'.claude/settings.json', {'apiKeyHelper': 'synthetic-command'}),
                            (self.home/'.claude/settings.local.json', {'env': {'CLAUDE_CONFIG_DIR': 'custom'}})):
            self.write(path, value)
            self.assertEqual(self.observe(), UNKNOWN)
            path.unlink()

    def test_bad_shapes_duplicates_oversize_and_nonprivate_credentials_are_unknown(self):
        for raw in ('null', '[]', '{broken', '{"claudeAiOauth":false}',
                    '{"claudeAiOauth":{"accessToken":123}}',
                    '{"claudeAiOauth":{"accessToken":"synthetic"},"claudeAiOauth":{}}',
                    '{"claudeAiOauth":{"expiresAt":Infinity}}',
                    ' ' * (FILE_LIMIT + 1)):
            self.write(self.credentials, raw, raw=True)
            self.assertEqual(self.observe(), UNKNOWN)
        self.write(self.credentials, {})
        self.credentials.chmod(0o644)
        self.assertEqual(self.observe(), UNKNOWN)

    def test_symlink_hardlink_fifo_or_directory_is_unknown_without_blocking(self):
        self.credentials.parent.mkdir(mode=0o700)
        victim = self.home/'owned-victim'
        self.write(victim, {})
        for kind in ('symlink', 'hardlink', 'fifo', 'directory'):
            if kind == 'symlink': self.credentials.symlink_to(victim)
            elif kind == 'hardlink': os.link(victim, self.credentials)
            elif kind == 'fifo': os.mkfifo(self.credentials, 0o600)
            else: self.credentials.mkdir()
            started = time.monotonic()
            self.assertEqual(self.observe(), UNKNOWN)
            self.assertLess(time.monotonic()-started, 1)
            if kind == 'directory': self.credentials.rmdir()
            else: self.credentials.unlink()

    def test_untrusted_ambient_temp_does_not_change_fixture_or_reader_trust(self):
        unsafe = self.home/'world-writable'
        unsafe.mkdir(mode=0o700)
        unsafe.chmod(0o777)  # Only this owned fixture, never a shared temp dir.
        with patch.object(tempfile, 'tempdir', str(unsafe)):
            nested = LocalAuthReadinessTests('test_absent_or_empty_oauth_is_negative_and_observation_never_creates_files')
            try:
                nested.setUp()
                self.assertFalse(nested.home.is_relative_to(unsafe))
                self.assertEqual(nested.observe(), MISSING)
            finally:
                nested.doCleanups()
        # Actual observations still refuse even a private leaf below an
        # untrusted writable ancestor; fixture relocation is not a bypass.
        with self.assertRaisesRegex(ValueError, 'untrusted_auth_location'):
            from hub2_agent_hub_host.local_auth_readiness import _read
            _read(unsafe/'absent.json')

    def test_observer_failure_is_unknown_and_never_logs_exception_or_contents(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                patch('hub2_agent_hub_host.local_auth_readiness._read', side_effect=OSError('synthetic-secret')):
            self.assertEqual(self.observe(), UNKNOWN)
        self.assertEqual(output.getvalue(), '')

    def scoped(self, workspace=None, **kw):
        return local_claude_missing_contexts([str(workspace or self.project)], ['h', 'i'], self.env,
            platform='linux', now=1800000000, filesystem_root=self.home, **kw)

    def test_scoped_marker_requires_exact_checked_cwd_and_checks_project_ancestors(self):
        self.project = self.home/'repos/project'
        self.project.mkdir(parents=True)
        flags = self.scoped()
        self.assertEqual(len(flags), 2)
        other=self.home/'other';other.mkdir()
        self.assertNotEqual(flags,self.scoped(other))
        self.assertTrue(all(len(flag) <= 80 and flag.endswith(':1800000000') for flag in flags))
        for directory in (self.project, self.project.parent, self.home):
            for name in ('settings.json', 'settings.local.json'):
                path = directory/'.claude'/name
                self.write(path, {'apiKeyHelper':'synthetic-never-executed-command'})
                self.assertEqual(self.scoped(), [], str(path))
                path.unlink()
        self.assertEqual(self.scoped(self.home/'absent'), [])
        alias = self.home/'alias'
        alias.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(self.scoped(alias), [])
        self.assertEqual(local_claude_missing_contexts(['project'], ['h'], self.env, platform='linux', filesystem_root=self.home), [])

    def test_project_alternative_env_managed_policy_and_failed_checks_stay_unknown(self):
        self.project=self.home/'project';self.project.mkdir()
        path=self.project/'.claude/settings.json'
        for value in ({'env':{'ANTHROPIC_API_KEY':'synthetic'}}, {'env':{'HOME':'other'}},
                      {'apiKeyHelper':['malformed']}, {'env':None}, {'env':{'UNRELATED':[]}}):
            self.write(path,value)
            self.assertEqual(self.scoped(), [])
        path.unlink()
        managed=self.home/'etc/claude-code/managed-settings.json'
        self.write(managed,{'apiKeyHelper':'synthetic-managed-helper'})
        self.assertEqual(self.scoped(), [])
        managed.unlink()
        fragments=managed.parent/'managed-settings.d';fragments.mkdir()
        self.assertEqual(self.scoped(), [])
        fragments.rmdir()
        fragments.symlink_to(self.home/'absent-fragment-directory')
        self.assertEqual(self.scoped(), [])
        fragments.unlink()
        path.symlink_to(self.home/'nonexistent')
        # A dangling settings symlink cannot become an absent configuration.
        self.assertEqual(self.scoped(), [])

    def test_outer_default_engine_environment_is_checked_for_both_launch_modes(self):
        self.project=self.home/'project';self.project.mkdir()
        config=self.home/'.config/aplexer/config.toml'
        self.write(config, 'default_engine="shell"\n[engines.shell]\ncommand=["/bin/sh"]\nenv={}\nenv_unset=[]\n',raw=True)
        self.assertEqual(len(self.scoped()),2, 'clear canonical outer shell preserves both negative observations')
        for name in ('CLAUDE_CONFIG_DIR','CLAUDE_CODE_OAUTH_TOKEN','HOME','PATH','XDG_CONFIG_HOME','APLEXER_CONFIG'):
            self.write(config, '[engines.shell]\ncommand=["/bin/sh"]\n[engines.shell.env]\n'+name+'="synthetic-alternate"\n',raw=True)
            self.assertEqual(self.scoped(),[],name)
            self.assertEqual(self.observe(),UNKNOWN,name)
            self.write(config, '[engines.shell]\ncommand=["/bin/sh"]\nenv_unset=['+json.dumps(name)+']\n',raw=True)
            self.assertEqual(self.scoped(),[],name+' unset')
        for value in ('default_engine="custom"\n[engines.custom]\ncommand=["/bin/sh"]\nenv={}\n',
                      'default_engine="custom"\n[engines.custom]\ncommand=["/bin/sh"]\n[engines.custom.env]\nCLAUDE_CONFIG_DIR="synthetic-alternate"\n',
                      'default_engine="claude"\n',
                      'default_profile="other"\n[profiles.other]\nengine="shell"\n[profiles.other.env]\nHOME="other"\n',
                      'shell="/bin/sh"\n',
                      '[engines.shell]\ncommand=["/bin/sh"]\nenv_unset="malformed"\n'):
            self.write(config,value,raw=True)
            self.assertEqual(self.scoped(),[])
        config.unlink()
        self.assertEqual(local_claude_missing_contexts([str(self.project)],['h','i'],
            {**self.env,'APLEXER_SHELL':'/bin/sh'},platform='linux',filesystem_root=self.home),[])
        self.assertEqual(len(self.scoped()),2)

    def test_context_count_depth_and_byte_budget_are_bounded(self):
        projects=[]
        for i in range(6):
            path=self.home/str(i);path.mkdir();projects.append(str(path))
        markers=local_claude_missing_contexts(projects,['h'],self.env,platform='linux',now=1800000000,filesystem_root=self.home)
        self.assertEqual(len(markers),4)
        deep=self.home.joinpath(*['x']*17);deep.mkdir(parents=True)
        self.project=deep
        self.assertEqual(self.scoped(),[])
        # More than the shared byte budget never yields a marker for an
        # incompletely inspected context (files individually remain legal).
        self.project=self.home/'a/b/c';self.project.mkdir(parents=True)
        for directory in (self.project,self.project.parent,self.project.parent.parent):
            for name in ('settings.json','settings.local.json'):
                self.write(directory/'.claude'/name,{'padding':'x'*200000})
        self.assertEqual(self.scoped(),[])


class OtherPlatformTests(unittest.TestCase):
    def test_unknown_platform_and_keychain_backends_do_not_read_local_files(self):
        for platform in ('darwin', 'win32', 'other'):
            with patch('hub2_agent_hub_host.local_auth_readiness._read', side_effect=AssertionError('must not read')):
                self.assertEqual(_default_claude_auth_state({}, platform=platform), UNKNOWN)


if __name__ == '__main__':
    unittest.main()
