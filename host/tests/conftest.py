import os
import tempfile

import pytest


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
    # Worktree-guard (AC1): git discovery from a test workspace must never
    # climb out of the temp tree into the developer's real checkout — a real
    # run locked the host's own linked worktree. Ceiling discovery at TMPDIR
    # (realpath'd: git compares canonical paths); repos built under TMPDIR
    # still self-identify via their own .git before walking up.
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES",
        os.path.realpath(os.environ.get("TMPDIR") or tempfile.gettempdir()))
