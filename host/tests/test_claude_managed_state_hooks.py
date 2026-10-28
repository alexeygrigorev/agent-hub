"""Managed-home claude sessions must be able to report semantic state.

The aplexer deferred-deliver guard only submits a pending Hub send when the
recipient has a fresh state-report push (waiting/working expire after 8s; a
Stop-hook `idle` is durable). Claude Code never reads .codex/hooks.json, so a
managed-home claude session without claude-format hooks can never push state
and Hub sends deadlock (no delivery -> no turn -> no Stop). The installed hook
set deliberately has NO Notification entry: claude's idle-reminder notification
fires while the session rests and its expiring `waiting` push would clobber
Stop's durable `idle`, re-creating the deadlock (observed live on the adopted
orchestrator-main session, intake 4f6cf562 AC4).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import provisioning as p

REPO_TMP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))


def _settings(home):
    return json.load(open(os.path.join(home, ".claude", "settings.json")))


def test_claude_managed_home_gets_state_hooks_without_notification():
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="claude-hooks-")
    try:
        home = os.path.join(base, "managed-home")
        os.makedirs(home)
        p.install_managed_engine_permissions(
            bundle={"provider": "claude"}, home=home, trusted_paths=[])
        settings = _settings(home)
        assert settings["skipDangerousModePermissionPrompt"] is True
        hooks = settings["hooks"]
        assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}, \
            "no Notification entry: its expiring `waiting` push would clobber durable idle"
        for event in ("SessionStart", "UserPromptSubmit"):
            commands = [h["command"] for group in hooks[event] for h in group["hooks"]]
            assert any("state-report working" in c for c in commands), event
        stop_commands = [h["command"] for group in hooks["Stop"] for h in group["hooks"]]
        assert any("state-report idle" in c for c in stop_commands)
        assert all("state-report working" not in c for c in stop_commands)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_existing_claude_settings_are_preserved():
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="claude-hooks-")
    try:
        home = os.path.join(base, "managed-home")
        claude_dir = os.path.join(home, ".claude")
        os.makedirs(claude_dir, mode=0o700)
        existing = {"model": "sonnet", "customKey": {"keep": True},
                    "permissions": {"deny": ["AskUserQuestion", "ExtraDeny"]}}
        settings_path = os.path.join(claude_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump(existing, f)
        os.chmod(settings_path, 0o600)
        p.install_managed_engine_permissions(
            bundle={"provider": "claude"}, home=home, trusted_paths=[])
        settings = _settings(home)
        assert settings["model"] == "sonnet"
        assert settings["customKey"] == {"keep": True}
        assert settings["permissions"]["deny"] == ["AskUserQuestion", "ExtraDeny"]
        assert "Stop" in settings["hooks"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_existing_custom_stop_hook_is_not_clobbered():
    base = tempfile.mkdtemp(dir=REPO_TMP, prefix="claude-hooks-")
    try:
        home = os.path.join(base, "managed-home")
        claude_dir = os.path.join(home, ".claude")
        os.makedirs(claude_dir, mode=0o700)
        custom_stop = {"hooks": [{"command": "/usr/bin/custom-stop-hook", "type": "command"}]}
        settings_path = os.path.join(claude_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"hooks": {"Stop": custom_stop}}, f)
        os.chmod(settings_path, 0o600)
        p.install_managed_engine_permissions(
            bundle={"provider": "claude"}, home=home, trusted_paths=[])
        hooks = _settings(home)["hooks"]
        assert hooks["Stop"] == custom_stop
        assert "UserPromptSubmit" in hooks
    finally:
        shutil.rmtree(base, ignore_errors=True)
