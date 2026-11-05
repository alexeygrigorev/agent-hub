"""Launcher-only AGENT_HUB_HOST_PATH (Hub intake 86254a30 AC6-8).

The maintained launcher imports managed probes only from a validated
AGENT_HUB_HOST_PATH. The host never set it, so any launcher after 61dea0f
failed closed for every managed Claude/Gemini/Antigravity launch. The host
now hands its own validated package root to launcher subprocesses in an
environment copy; its own environment and sys.path stay untouched, and a
failed proof removes the variable rather than forwarding an ambient value.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_hub_host
from agent_hub_host import fleet, launcher_env


def test_host_path_is_the_running_packages_real_root():
    expected = os.path.realpath(Path(agent_hub_host.__file__).resolve().parent.parent)
    assert launcher_env.host_path() == expected
    assert os.path.isabs(expected)
    assert (Path(expected) / "agent_hub_host" / "__init__.py").is_file()


def test_environment_copy_only_and_no_global_mutation(monkeypatch):
    monkeypatch.delenv(launcher_env.VAR, raising=False)
    before_path = list(sys.path)
    env = launcher_env.launcher_environment()
    assert env[launcher_env.VAR] == launcher_env.host_path()
    assert launcher_env.VAR not in os.environ
    assert sys.path == before_path
    assert env is not os.environ


def test_failed_proof_removes_ambient_value():
    ambient = {launcher_env.VAR: "C:\\evil" if os.name == "nt" else "/tmp/evil", "KEEP": "1"}
    with patch.object(launcher_env, "host_path", return_value=None):
        env = launcher_env.launcher_environment(ambient)
    assert launcher_env.VAR not in env and env["KEEP"] == "1"
    # A valid proof also overrides any ambient value.
    env = launcher_env.launcher_environment(ambient)
    assert env[launcher_env.VAR] == launcher_env.host_path()


def test_package_mismatch_fails_proof():
    with patch.object(agent_hub_host, "__file__", str(Path(agent_hub_host.__file__).parent / "elsewhere.py")):
        assert launcher_env.host_path() is None


def _dispatcher(runner):
    d = fleet.Dispatcher.__new__(fleet.Dispatcher)
    d.runner = runner
    return d


def test_launcher_run_passes_env_only_to_env_aware_runners():
    seen = []

    def aware(argv, **kw):
        seen.append(("aware", kw))
        return {}
    aware.accepts_env = True

    def legacy(argv, **kw):
        seen.append(("legacy", kw))
        return {}

    _dispatcher(aware)._launcher_run(["launcher", "run"], cwd="x", timeout=5)
    _dispatcher(legacy)._launcher_run(["launcher", "run"], cwd="x")
    assert seen[0][1]["env"][launcher_env.VAR] == launcher_env.host_path()
    assert seen[0][1]["cwd"] == "x" and seen[0][1]["timeout"] == 5
    assert seen[1] == ("legacy", {"cwd": "x"})


def test_run_json_forwards_env_to_popen_and_skips_native_client():
    captured = {}

    class Stop(Exception):
        pass

    def popen(argv, **kw):
        captured.update(kw)
        raise Stop()

    with patch.object(subprocess, "Popen", side_effect=popen), \
            patch("agent_hub_host.aplexer_client.client_call", side_effect=AssertionError("no native client")):
        with pytest.raises(Stop):
            fleet.run_json(["launcher", "plan"], env={"A": "1"})
    assert captured["env"] == {"A": "1"}


def test_only_headless_launcher_run_site_uses_launcher_env():
    src = Path(fleet.__file__).read_text(encoding="utf-8")
    assert """interactive_effect_invoked = True
                native_start_invoked = True
                receipt = self.runner(argv, cwd=cwd, timeout=120)
            else:
                receipt = self._launcher_run(argv, cwd=cwd, timeout=120)""" in src
    for site in ('self._launcher_run(submit_argv, cwd=launcher["root"])',
                 'self._launcher_run(reconcile_argv, cwd=launcher["root"])',
                 'self._preclaim_check(offer_id, self._launcher_run, prefix + ["plan", "--id", task_id]'):
        assert site in src
