"""Git environment for Hub2 evidence commands; never allow a prompt to hang a session."""
from __future__ import annotations

import os
import shutil
import sys


def noninteractive_env(base=None):
    env = dict(os.environ if base is None else base)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="true",
               SSH_ASKPASS="true", SSH_ASKPASS_REQUIRE="never", LC_ALL="C")
    if sys.platform == "win32":
        try:
            count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        except ValueError:
            count = 0
        env[f"GIT_CONFIG_KEY_{count}"] = "credential.helper"
        env[f"GIT_CONFIG_VALUE_{count}"] = ""
        count += 1
        gh = shutil.which("gh")
        if gh:
            env[f"GIT_CONFIG_KEY_{count}"] = "credential.helper"
            env[f"GIT_CONFIG_VALUE_{count}"] = '!"' + gh.replace("\\", "/") + '" auth git-credential'
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
    return env
