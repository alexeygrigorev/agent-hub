"""Hard guard and human-profile protection (intake 6b624e65 AC2).

The 2026-10-27 incident: a plain-unittest host suite run with the real HOME
wrote fixture tokens into the real human cli.json. These tests pin the two
defenses: (1) once the hermetic-suite guard is armed, config access to the
recorded real user profile raises even after environment mocks; (2) outside
tests, a valid human profile is never replaced by a tokenless profile or a
host/agent credential, and update_config reads and writes exactly the one
profile path it resolved.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agent_hub_host import config


class TestSuiteIsolationArmed(unittest.TestCase):
    def test_host_test_process_runs_hermetic(self):
        # AC1: isolation ran at package/conftest import, before agent_hub_host.
        guarded_home = os.environ.get(config.TEST_GUARD_HOME_ENV)
        self.assertTrue(guarded_home, "suite guard must record the pre-isolation home")
        home = os.environ.get("HOME")
        self.assertTrue(home)
        self.assertNotEqual(Path(home).resolve(), Path(guarded_home).resolve())
        self.assertTrue(Path(home).is_dir())
        for name in ("HUB_CONFIG_FILE", "HUB_URL", "HUB_BASE_URL"):
            self.assertNotIn(name, os.environ)

    def test_bootstrapped_guard_survives_environment_clear_and_replacement(self):
        script = r'''
import json, os, sys
from pathlib import Path
home = Path(sys.argv[1])
cli = home / '.config/agent-hub/cli.json'
host = cli.with_name('host.json')
before = {p: p.read_bytes() for p in (cli, host)}
if sys.argv[3] == 'inherited':
    os.environ['HUB_TEST_GUARD_HOME'] = str(home)
    os.environ['HUB_TEST_GUARD_XDG_CONFIG_HOME'] = str(home / '.config')
else:
    import unittest
from agent_hub_host import config, _test_isolation
initial_blocked = False
try:
    config._load_from(cli)
except config.ConfigPermissionError:
    initial_blocked = True
if sys.argv[2] == 'clear':
    os.environ.clear()
else:
    for key in (config.TEST_GUARD_HOME_ENV, config.TEST_GUARD_USER_HOME_ENV,
                config.TEST_GUARD_XDG_ENV, config.TEST_GUARD_APPDATA_ENV):
        os.environ[key] = str(home.parent / 'other-home')
# Resolve only synthetic profiles, including on the vulnerable version.
os.environ.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                  APPDATA=str(home / '.config'))
actions = {
    'host_path': config.get_config_path,
    'human_path': config.get_human_config_path,
    'read': lambda: config._load_from(cli),
    'write': lambda: config._save_to(host, {'client_kind': 'host',
                                          'access_token': 'synthetic-new'}),
    'clear_host': config.clear_config,
    'clear_human': config.clear_human_config,
}
blocked = {}
for name, action in actions.items():
    try:
        action()
    except config.ConfigPermissionError:
        blocked[name] = True
    else:
        blocked[name] = False
print(json.dumps({'initial_blocked': initial_blocked, 'blocked': blocked,
                  'runner_loaded': _test_isolation.under_test_runner(),
                  'unchanged': all(p.exists() and p.read_bytes() == data
                                   for p, data in before.items())}))
'''
        for bootstrap in ("runner", "inherited"):
            for mutation in ("clear", "replace"):
                with self.subTest(bootstrap=bootstrap, mutation=mutation):
                    with tempfile.TemporaryDirectory() as directory:
                        base = Path(directory)
                        home = base / "canary-home"
                        profiles = home / ".config" / "agent-hub"
                        profiles.mkdir(parents=True, mode=0o700)
                        for name, kind in (("cli.json", "human"), ("host.json", "host")):
                            path = profiles / name
                            path.write_text(json.dumps({"client_kind": kind,
                                                        "access_token": "synthetic-canary"}))
                            path.chmod(0o600)
                        scratch = base / "tmp"
                        scratch.mkdir()
                        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
                               if key in os.environ}
                        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                                   APPDATA=str(home / ".config"), TMPDIR=str(scratch),
                                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                        result = subprocess.run(
                            [sys.executable, "-c", script, str(home), mutation, bootstrap],
                            env=env, text=True, capture_output=True, timeout=30,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        data = json.loads(result.stdout)
                        self.assertTrue(data["initial_blocked"])
                        self.assertEqual(data["runner_loaded"], bootstrap == "runner")
                        self.assertTrue(all(data["blocked"].values()), data["blocked"])
                        self.assertTrue(data["unchanged"])


class TestRealProfileGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.real_home = Path(self.tmp.name) / "real-home"
        self.real_config = self.real_home / ".config" / "agent-hub"
        self.real_config.mkdir(parents=True, mode=0o700)
        self.real_cli = self.real_config / "cli.json"
        self.real_cli.write_text('{"client_kind": "human", "access_token": "fixture-real"}')
        self.real_cli.chmod(0o600)
        self.free = Path(self.tmp.name) / "free"
        self.free.mkdir(mode=0o700)
        self.guard = patch.dict(os.environ, {config.TEST_GUARD_HOME_ENV: str(self.real_home)})

    def test_path_resolvers_refuse_real_profile_dir(self):
        with self.guard:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.real_home / ".config")}):
                for resolver in (config.get_config_path, config.get_human_config_path):
                    with self.assertRaises(config.ConfigPermissionError):
                        resolver()
            with patch.dict(os.environ, {"HUB_CONFIG_FILE": str(self.real_cli)}):
                for resolver in (config.get_config_path, config.get_human_config_path):
                    with self.assertRaises(config.ConfigPermissionError):
                        resolver()

    def test_primitives_refuse_real_profile_paths_but_allow_temp_paths(self):
        inside = self.real_config / "host.json"
        outside = self.free / "host.json"
        with self.guard:
            with self.assertRaises(config.ConfigPermissionError):
                config._load_from(inside)
            with self.assertRaises(config.ConfigPermissionError):
                config._save_to(inside, {"access_token": "fixture-x"})
            config._save_to(outside, {"access_token": "fixture-x"})
            self.assertEqual(config._load_from(outside).get("access_token"), "fixture-x")
        # Without the guard env the same paths behave normally.
        config._save_to(inside, {"access_token": "fixture-x"})
        self.assertEqual(config._load_from(inside).get("access_token"), "fixture-x")

    def test_account_home_remains_guarded_when_home_was_already_disposable(self):
        with patch.dict(os.environ, {
            config.TEST_GUARD_HOME_ENV: str(self.free),
            config.TEST_GUARD_USER_HOME_ENV: str(self.real_home),
        }):
            with self.assertRaises(config.ConfigPermissionError):
                config._load_from(self.real_cli)
            with self.assertRaises(config.ConfigPermissionError):
                config._save_to(self.real_cli, {"access_token": "fixture-x"})


class TestHumanProfileOverwriteProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cli = Path(self.tmp.name) / "config" / "cli.json"
        self.cli.parent.mkdir(mode=0o700)
        patcher = patch.object(config, "get_human_config_path", return_value=self.cli)
        self.mock_path = patcher.start()
        self.addCleanup(patcher.stop)

    def human(self, token="fixture-human-token"):
        return {"client_kind": "human", "access_token": token, "base_url": "https://hub.example"}

    def test_valid_human_profile_refuses_tokenless_and_foreign_kind(self):
        config.save_human_config(self.human())
        before = self.cli.read_bytes()
        for hostile in (
            {"client_kind": "human"},                                   # tokenless
            {"client_kind": "human", "access_token": ""},               # empty token
            {"client_kind": "agent", "access_token": "fixture-agent"},  # different flow
            {"client_kind": "host", "access_token": "fixture-host"},
            {"access_token": "fixture-kindless"},
        ):
            with self.subTest(hostile=hostile):
                with self.assertRaises(config.ConfigPermissionError):
                    config.save_human_config(hostile)
        self.assertEqual(self.cli.read_bytes(), before)

    def test_human_login_and_first_login_still_save(self):
        config.save_human_config({"base_url": "https://hub.example"})  # fresh, no profile yet
        config.save_human_config(self.human("fixture-first"))
        self.assertEqual(config.load_human_config()["access_token"], "fixture-first")
        config.save_human_config(self.human("fixture-rotated"))  # human re-login keeps kind
        self.assertEqual(config.load_human_config()["access_token"], "fixture-rotated")


class TestUpdateConfigSingleProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host_json = Path(self.tmp.name) / "config" / "host.json"
        self.host_json.parent.mkdir(mode=0o700)

    def test_update_config_never_writes_discovered_worker_profile(self):
        # The 2026-10-27-adjacent hazard: update_config read host.json but the
        # legacy save_config fell through to the per-launch worker profile.
        workspace = Path(self.tmp.name) / "ws"
        workspace.mkdir()
        worker = workspace / ".local" / "hub-worker-auth" / "00000000-0000-4000-8000-000000000000.json"
        with patch.object(config, "get_config_path", return_value=self.host_json), \
                patch.dict(os.environ, {"APLEXER_TAG": "hub-00000000-0000-4000-8000-000000000000",
                                        "APLEXER_WORKSPACE": str(workspace)}):
            config.update_config(access_token="fixture-host-token", machine_id="fixture-machine")
        self.assertEqual(config._load_from(self.host_json).get("machine_id"), "fixture-machine")
        self.assertFalse(worker.exists())

    def test_update_config_single_file_override_writes_exactly_that_file(self):
        override = Path(self.tmp.name) / "override.json"
        with patch.dict(os.environ, {"HUB_CONFIG_FILE": str(override)}), \
                patch.object(config, "get_config_path", return_value=override):
            config.update_config(access_token="fixture-override-token")
        self.assertEqual(config._load_from(override).get("access_token"), "fixture-override-token")
        self.assertTrue(override.exists())
        self.assertFalse(self.host_json.exists())


if __name__ == "__main__":
    unittest.main()
