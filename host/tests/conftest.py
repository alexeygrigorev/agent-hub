import os
import tempfile
from pathlib import Path

import pytest


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


def _real_worker_profile_dir():
    """The checkout's real per-launch profile dir (never a test target)."""
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return None
    return root / ".local" / "hub-worker-auth"


def _snapshot_profile_dir(path):
    if path is None or not path.is_dir():
        return None
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


@pytest.fixture(autouse=True, scope="session")
def guard_real_worker_profiles():
    """Fail the suite if any test touches the real hub-worker-auth dir.

    Compares mtime/size snapshots only; profile contents (tokens) are
    never read. Any addition, removal or rewrite — including the
    .refresh.lock sidecars — fails with the affected file names.
    """
    path = _real_worker_profile_dir()
    before = _snapshot_profile_dir(path)
    yield
    if before is None:
        return
    after = _snapshot_profile_dir(path)
    if after is None:
        pytest.fail("real hub-worker-auth directory disappeared during the test session")
    changed = sorted(
        name for name in set(before) | set(after)
        if before.get(name) != after.get(name))
    assert not changed, (
        "test session touched the real hub-worker-auth profiles (never a test target): "
        + ", ".join(changed))
