"""The canary gate must fail closed when suites fail or profiles change."""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'ci' / 'host_canary_check.py'
_SPEC = importlib.util.spec_from_file_location('host_canary_check', _SCRIPT)
canary = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(canary)


class CanaryHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {'TMPDIR': self.tmp.name})
        environment.start()
        self.addCleanup(environment.stop)
        prefix = patch.object(canary, 'uv_prefix', return_value=[sys.executable])
        prefix.start()
        self.addCleanup(prefix.stop)

    def run_gate(self, run):
        with patch.object(canary, 'run_suite', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            return canary.main()

    @staticmethod
    def successful_run(name, cwd, args, env, log_path=None):
        if log_path is not None:
            log_path.write_text('Ran 1 test in 0.001s\n\nOK\n')
        return 0

    def test_nonzero_suite_status_never_passes(self):
        for failed in ('hub CLI zipapp build', 'pytest discovery (repo root)', 'unittest discover (host/)'):
            with self.subTest(suite=failed):
                def run(name, cwd, args, env, log_path=None):
                    self.successful_run(name, cwd, args, env, log_path)
                    return int(name == failed)
                self.assertEqual(self.run_gate(run), 1)

    def test_either_profile_mutation_fails_even_if_suites_pass(self):
        for filename in ('cli.json', 'host.json'):
            with self.subTest(profile=filename):
                def run(name, cwd, args, env, log_path=None):
                    self.successful_run(name, cwd, args, env, log_path)
                    if name == 'pytest discovery (repo root)':
                        path = Path(env['HOME']) / '.config' / 'agent-hub' / filename
                        path.write_text('{}\n')
                    return 0
                self.assertEqual(self.run_gate(run), 1)

    def test_success_cleans_only_its_own_temporary_directory(self):
        sentinel = Path(self.tmp.name) / 'preexisting-sibling'
        sentinel.write_text('preserve')
        homes = []
        def run(name, cwd, args, env, log_path=None):
            homes.append(Path(env['HOME']))
            return self.successful_run(name, cwd, args, env, log_path)
        self.assertEqual(self.run_gate(run), 0)
        self.assertTrue(homes)
        self.assertTrue(all(not home.exists() for home in homes))
        self.assertEqual(sentinel.read_text(), 'preserve')

    def test_ambient_selectors_cannot_bypass_the_canary_home(self):
        inherited = {key: '/fixture/ambient' for key in (
            'HOME', 'XDG_CONFIG_HOME', 'APPDATA', 'HUB_CONFIG_FILE', 'HUB_WORKER_CONFIG_FILE',
            'HUB_TEST_GUARD_HOME', 'HUB_URL', 'HUB_BASE_URL', 'APLEXER_TAG', 'APLEXER_WORKSPACE')}
        with patch.dict(os.environ, inherited):
            env = canary.suite_env(Path(self.tmp.name))
        self.assertEqual(env['HOME'], str(Path(self.tmp.name) / 'canary-home'))
        for key in inherited.keys() - {'HOME'}:
            self.assertNotIn(key, env)
        self.assertIn(str(canary.REPO_ROOT), env['PYTHONPATH'].split(os.pathsep))

    @unittest.skipUnless(shutil.which('git'), 'Git is required for discovery regression')
    def test_git_discovery_stops_at_scratch_but_finds_synthetic_repositories(self):
        parent = Path(self.tmp.name) / 'parent-repository'
        scratch = parent / 'canary-run'
        plain = scratch / 'tmp' / 'plain'
        plain.mkdir(parents=True)
        with patch.dict(os.environ):
            os.environ.pop('GIT_CEILING_DIRECTORIES', None)
            env = canary.suite_env(scratch)
        init = subprocess.run(['git', 'init', '-q'], cwd=parent, env=env,
                              capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr)
        probe = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                               cwd=plain, env=env, capture_output=True, text=True)
        self.assertNotEqual(probe.returncode, 0,
                            'synthetic directory discovered its parent checkout')
        repo = scratch / 'tmp' / 'repo'
        repo.mkdir()
        init = subprocess.run(['git', 'init', '-q'], cwd=repo, env=env,
                              capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr)
        probe = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                               cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(Path(probe.stdout.strip()).resolve(), repo.resolve())

    def test_relative_run_paths_survive_the_unittest_working_directory(self):
        relative = Path(os.path.relpath(self.tmp.name, Path.cwd()))
        env = canary.suite_env(relative)
        for key, suffix in (('HOME', 'canary-home'), ('TMPDIR', 'tmp')):
            with self.subTest(variable=key):
                self.assertTrue(Path(env[key]).is_absolute())
                self.assertEqual(Path(env[key]), Path(self.tmp.name) / suffix)


if __name__ == '__main__':
    unittest.main()
