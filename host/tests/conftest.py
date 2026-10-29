import os
import tempfile
from pathlib import Path

import pytest

# Hermetic-by-default (intake 6b624e65 AC1): runs at conftest import time,
# i.e. before pytest imports any test module that imports agent_hub_host.
# unittest discovery gets the same isolation from host/tests/__init__.py and
# from agent_hub_host._test_isolation.auto_isolate().
from agent_hub_host._test_isolation import isolate as _isolate_hermetic_home

_isolate_hermetic_home()


def _native_launch_identity_vars():
    """All env selectors that resolve the live per-launch worker profile.

    worker_auth.config_path() follows HUB_WORKER_CONFIG_FILE explicitly,
    otherwise APLEXER_TAG plus APLEXER_WORKSPACE (or CWD git discovery).
    None of these may survive into a test: a suite run from an aplexer
    session shell would otherwise read and overwrite that session's real
    hub-worker-auth credential (incident edb71968: 20 live profiles
    clobbered with the test_machine_binding fixture).
    """
    names = ["HUB_WORKER_CONFIG_FILE", "APLEXER_TAG", "APLEXER_WORKSPACE",
             "APLEXER_SESSION_ID"]
    names.extend(v for v in os.environ if v.startswith("APLEXER_") and v not in names)
    return names


def _snapshot_profile_dir(path):
    if path is None or not path.is_dir():
        return {}
    snap = {}
    for child in path.iterdir():
        try:
            st = child.stat()
        except OSError:
            continue
        if child.is_file() or child.is_symlink():
            snap[child.name] = (st.st_mtime_ns, st.st_size)
    return snap


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    """Keep tests from reading or writing the real ~/.config/agent-hub (credentials, settings)."""
    config_home = tmp_path / "xdg-config"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.delenv("HUB_CONFIG_FILE", raising=False)
    for name in ("HUB_URL", "HUB_BASE_URL", "HUB_TOKEN", "HUB_JOB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    # Worker-profile guard (incident edb71968): a disposable HOME does not
    # isolate the per-launch credential, which is resolved from native
    # launch identity instead of HOME/XDG. Clear every selector so
    # worker_auth.config_path() is None in every test unless the test
    # itself opts into an explicit tmp workspace via patch.dict.
    for name in _native_launch_identity_vars():
        monkeypatch.delenv(name, raising=False)
    # Worktree-guard (AC1): git discovery from a test workspace must never
    # climb out of the temp tree into the developer's real checkout — a real
    # run locked the host's own linked worktree. Ceiling discovery at TMPDIR
    # (realpath'd: git compares canonical paths); repos built under TMPDIR
    # still self-identify via their own .git before walking up.
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES",
        os.path.realpath(os.environ.get("TMPDIR") or tempfile.gettempdir()))


@pytest.fixture(autouse=True)
def guard_worker_profile_writes(monkeypatch):
    """Reject profile writes outside this run's temp tree before any I/O."""
    from agent_hub_host import private_fs
    original = private_fs.atomic_write
    temporary = Path(tempfile.gettempdir()).resolve()

    def guarded(path, raw, limit, **kwargs):
        target = Path(path)
        if "hub-worker-auth" in target.parts:
            assert target.resolve().is_relative_to(temporary), (
                "test attempted a real hub-worker-auth write: " + str(target))
        return original(path, raw, limit, **kwargs)

    monkeypatch.setattr(private_fs, "atomic_write", guarded)


@pytest.fixture(autouse=True, scope="session")
def guard_real_worker_profiles():
    """Watch this checkout; other live checkouts may legitimately refresh.

    The pre-I/O guard above protects ALL non-temp profile paths, including
    the canonical checkout. Watching its shared directory here would also
    fail on unrelated real workers provisioning or refreshing credentials.
    """
    directory = Path(__file__).resolve().parents[2] / ".local/hub-worker-auth"
    before = _snapshot_profile_dir(directory)
    yield
    after = _snapshot_profile_dir(directory)
    changed = sorted(name for name in set(before) | set(after)
                     if before.get(name) != after.get(name))
    assert not changed, (
        "test session touched real hub-worker-auth profiles: " + ", ".join(changed))


@pytest.fixture(autouse=True)
def isolated_host_load(monkeypatch):
    """Unit dispatch uses a stable host; overload tests inject their own samples.

    Other sessions' live load must not decide which mocked launch path runs.
    Patch only this module's OS reader, preserving the real gate and every
    explicit numeric/error sample in the dedicated load and dispatch tests.
    """
    from types import SimpleNamespace
    from agent_hub_host import host_load
    monkeypatch.setattr(host_load, "os", SimpleNamespace(
        getloadavg=lambda: (0.0, 0.0, 0.0), cpu_count=lambda: 12))
