import os

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
