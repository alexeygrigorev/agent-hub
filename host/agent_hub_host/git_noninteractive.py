"""Environment that makes every Hub-launched git non-interactive (intake 446227dc).

Alexey: "when you launch agents on windows make sure the terminal window
doesn't pop up because it's distracting."

Git for Windows' bundled system config selects ``credential.helper`` through
``git-credential-helper-selector.exe`` when no helper is configured; it opens
a modal "Select a credential helper" dialog and hangs a clone-on-demand
forever. Hub-launched git (host clone/fetch/worktree and managed sessions)
must therefore never prompt and never reach the selector:

* ``GIT_TERMINAL_PROMPT=0``, ``GCM_INTERACTIVE=never``, a no-op ``GIT_ASKPASS``
  and ``SSH_ASKPASS`` so no prompt of any kind is possible;
* on Windows the helper list is reset (``credential.helper=``, which drops the
  selector) and replaced by ``!gh auth git-credential`` so the host's own gh
  login (keyring) supplies the token. Nothing is read from gh's files and no
  token is stored or printed here.

The config is injected through ``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n`` so it
also reaches git-remote-https and every other git child.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Mapping, Optional

AUTH_FAILED = "repo_clone_auth_failed"
_AUTH_MARKERS = (
    "terminal prompts disabled", "could not read username", "could not read password",
    "authentication failed", "invalid credentials", "permission denied", "http 401",
    "returned error: 403", "returned error: 401", "repository not found", "bad credentials",
    "no credential helper", "credential-manager", "host key verification failed",
)


def credential_helpers(gh_bin: Optional[str] = None) -> list[str]:
    """Ordered ``credential.helper`` values: reset first, then the gh helper."""
    helpers = [""]
    gh = gh_bin if gh_bin is not None else shutil.which("gh")
    if gh:
        helpers.append('!"' + gh.replace("\\", "/") + '" auth git-credential')
    return helpers


def noninteractive_env(base: Optional[Mapping[str, str]] = None, *, platform: Optional[str] = None,
                       gh_bin: Optional[str] = None) -> dict[str, str]:
    """Copy of ``base`` (default os.environ) that makes git never prompt."""
    env = dict(os.environ if base is None else base)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="true",
               SSH_ASKPASS="true", SSH_ASKPASS_REQUIRE="never", LC_ALL="C")
    if (platform or sys.platform) == "win32":
        try:
            count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        except ValueError:
            count = 0
        for helper in credential_helpers(gh_bin):
            env[f"GIT_CONFIG_KEY_{count}"] = "credential.helper"
            env[f"GIT_CONFIG_VALUE_{count}"] = helper
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
    return env


def session_env_pairs(*, platform: Optional[str] = None, gh_bin: Optional[str] = None) -> list[tuple[str, str]]:
    """``(name, value)`` pairs for a managed session's environment (Windows only config)."""
    if (platform or sys.platform) != "win32":
        return []
    base: dict[str, str] = {}
    full = noninteractive_env(base, platform=platform, gh_bin=gh_bin)
    full.pop("LC_ALL", None)
    return sorted(full.items())


def is_auth_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)
