"""Hermetic HOME isolation for the host test suite (intake 6b624e65 AC1).

The 2026-10-27 incident: a plain-unittest host suite run with the real HOME
resolved the human-first active profile and wrote fixture tokens into the
real ~/.config/agent-hub/cli.json. This module makes every host test run
hermetic by default:

1. Records where the real user profiles lived (HOME / XDG_CONFIG_HOME /
   APPDATA as found before isolation) into HUB_TEST_GUARD_* environment
   variables for child processes and retains them in-process, so environment
   mocks cannot remove the agent_hub_host.config hard guard (AC2).
2. Points HOME, XDG_CONFIG_HOME and APPDATA at a fresh temporary directory
   and clears ambient Hub connection variables, so no test can resolve the
   real ~/.config/agent-hub/{cli,host}.json of the real user (AC1).

Arming happens from host/tests/conftest.py (pytest), host/tests/__init__.py
(unittest package imports) and from agent_hub_host/__init__.py whenever a
test runner (unittest / pytest) is already in sys.modules - bare-module
unittest discovery never imports the tests package, so the package-level
arming is what makes `python -m unittest discover -s tests` hermetic too.
The real hub CLI never imports unittest or pytest, so production flows are
never isolated. Idempotent: the first isolate() in a process wins.
"""
import atexit
import os
import sys
import tempfile

GUARD_HOME_ENV = "HUB_TEST_GUARD_HOME"
GUARD_USER_HOME_ENV = "HUB_TEST_GUARD_USER_HOME"
GUARD_XDG_ENV = "HUB_TEST_GUARD_XDG_CONFIG_HOME"
GUARD_APPDATA_ENV = "HUB_TEST_GUARD_APPDATA"

_GUARD_NAMES = (GUARD_HOME_ENV, GUARD_USER_HOME_ENV, GUARD_XDG_ENV, GUARD_APPDATA_ENV)
_guarded_environment: tuple[tuple[str, str], ...] = ()


def _remember_guard_environment():
    global _guarded_environment
    if not _guarded_environment and os.environ.get(GUARD_HOME_ENV):
        _guarded_environment = tuple(
            (name, os.environ[name]) for name in _GUARD_NAMES if os.environ.get(name)
        )


def guard_environment() -> dict[str, str]:
    """Original guard roots, retained even when a test replaces its environment."""
    return dict(_guarded_environment)

# Ambient connection/credential selectors: any of these left over from the
# launching session would make config resolution leave the isolated home.
# APLEXER_TAG / APLEXER_WORKSPACE additionally expose the per-launch worker
# profile (the same real-credential class this intake guards).
_CLEARED = (
    "HUB_CONFIG_FILE", "HUB_URL", "HUB_BASE_URL", "HUB_TOKEN", "HUB_JOB_TOKEN",
    "HUB_WORKER_CONFIG_FILE",
    "APLEXER_TAG", "APLEXER_WORKSPACE",
)


def under_test_runner() -> bool:
    """True when a test runner is active in this interpreter."""
    return any(name in sys.modules for name in ("unittest", "pytest", "_pytest"))


def isolate():
    if _guarded_environment:
        return
    if os.environ.get(GUARD_HOME_ENV):
        _remember_guard_environment()
        return  # Already isolated in this process.
    os.environ[GUARD_HOME_ENV] = os.path.expanduser("~")
    # A runner may already have replaced HOME. Keep the account's native
    # home protected too, without reading any profile contents.
    if os.name == "posix":
        import pwd
        os.environ[GUARD_USER_HOME_ENV] = pwd.getpwuid(os.getuid()).pw_dir
    elif os.environ.get("USERPROFILE"):
        os.environ[GUARD_USER_HOME_ENV] = os.environ["USERPROFILE"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        os.environ[GUARD_XDG_ENV] = xdg
    appdata = os.environ.get("APPDATA")
    if appdata:
        os.environ[GUARD_APPDATA_ENV] = appdata
    # Environment mocks must not erase the protection armed by this bootstrap.
    _remember_guard_environment()
    owner = tempfile.TemporaryDirectory(prefix="hub-tests-home-")
    atexit.register(owner.cleanup)
    home = owner.name
    os.environ["HOME"] = home
    os.environ["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    os.environ["APPDATA"] = os.path.join(home, ".config")
    for name in _CLEARED:
        os.environ.pop(name, None)


def auto_isolate():
    """Arm the isolation when imported by a test runner process."""
    # CLI subprocesses can inherit an armed guard without importing a runner.
    _remember_guard_environment()
    if under_test_runner():
        isolate()
