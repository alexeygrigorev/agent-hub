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
from typing import Callable, Mapping, Optional

AUTH_FAILED = "repo_clone_auth_failed"
_AUTH_MARKERS = (
    "terminal prompts disabled", "could not read username", "could not read password",
    "authentication failed", "invalid credentials", "permission denied", "http 401",
    "returned error: 403", "returned error: 401", "bad credentials",
    "no credential helper", "credential-manager", "host key verification failed",
)


def find_gh() -> Optional[str]:
    """gh executable: PATH first, then the standard Windows install locations."""
    found = shutil.which("gh")
    if found:
        return found
    for var, tail in (("ProgramFiles", "GitHub CLI"), ("LOCALAPPDATA", "Programs/GitHub CLI")):
        base = os.environ.get(var)
        if base:
            candidate = os.path.join(base, tail, "gh.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def credential_helpers(gh_bin: Optional[str] = None) -> list[str]:
    """Ordered ``credential.helper`` values: reset first, then the gh helper."""
    helpers = [""]
    gh = gh_bin if gh_bin is not None else find_gh()
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


def is_repo_not_found(text: str) -> bool:
    """Recognize GitHub's fixed missing-origin response without forwarding it."""
    lowered = (text or "").lower()
    return "repository not found" in lowered or "remote repository does not exist" in lowered


_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
_ALL_PROXY_ENV = ("ALL_PROXY", "all_proxy")
_NO_PROXY_ENV = ("NO_PROXY", "no_proxy")
_GITHUB_SUFFIXES = ("github.com",)
_GH_EXTRA_HOSTS = ("api.github.com", "objects.githubusercontent.com",
                   "codeload.github.com", "raw.githubusercontent.com")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOOPBACK_NET = "127."


def _proxy_host_port(value: str) -> Optional[tuple[str, str]]:
    """Split a proxy URL into a sanitized ``(host, port-or-'')`` pair.

    Credentials, scheme details and paths never leave this helper: only the
    host and port are reported so a dead proxy is diagnosable without leaking
    proxy auth material.
    """
    text = (value or "").strip()
    if not text:
        return None
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if not text:
        return None
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    else:
        host, port = text, ""
    host = host.strip().lower().rstrip(".")
    if not host or any(ord(c) < 33 or ord(c) == 127 for c in host + port):
        return None
    return host, port.strip()


def _no_proxy_bypasses(entries: str, host: str) -> bool:
    """Whether a NO_PROXY list bypasses the proxy for ``host``."""
    target = (host or "").strip().lower().rstrip(".")
    if not target:
        return False
    for entry in (entries or "").split(","):
        rule = entry.strip().lower().rstrip(".")
        if not rule:
            continue
        if rule == "*":
            return True
        # Strip an optional :port; matching is host-only.
        if ":" in rule and not rule.startswith("["):
            rule = rule.split(":", 1)[0].rstrip(".")
        if rule.startswith("*."):
            rule = rule[1:]
        if rule.startswith("."):
            if target == rule[1:] or target.endswith(rule):
                return True
        elif target == rule or target.endswith("." + rule):
            return True
    return False


def dead_proxy_finding(env: Optional[Mapping[str, str]] = None,
                       opener: Optional[Callable[[str, int], None]] = None,
                       hosts: tuple = _GITHUB_SUFFIXES) -> Optional[str]:
    """Name a configured proxy that cannot serve github hosts, else None.

    Only loopback proxies are liveness-checked (a fast local TCP connect):
    a remote proxy may legitimately refuse our probe while serving git. A
    loopback proxy with nothing listening means git to github fails and every
    clone would report a bare ``repo_clone_failed``. The finding carries only
    a sanitized host:port, never credentials. ``hosts`` lists the GitHub hosts
    the attempted clone path needs (``gh`` also fetches release objects and
    the API); a proxy bypassed for all of them via NO_PROXY is left alone so
    an operator NO_PROXY workaround keeps working.
    """
    wanted = tuple(h for h in (hosts or ()) if h) or _GITHUB_SUFFIXES
    source = dict(os.environ if env is None else env)
    bypass = source.get("NO_PROXY", source.get("no_proxy", ""))
    if all(_no_proxy_bypasses(bypass, host) for host in wanted):
        return None
    candidates: list[str] = []
    for key in _PROXY_ENV + _ALL_PROXY_ENV:
        raw = source.get(key)
        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())
    for raw in candidates:
        parsed = _proxy_host_port(raw)
        if parsed is None:
            continue
        host, port = parsed
        loopback = (host in _LOOPBACK_HOSTS or host.startswith(_LOOPBACK_NET)
                    or host in ("::ffff:127.0.0.1",))
        if not loopback:
            continue
        if all(_no_proxy_bypasses(bypass, host) for host in wanted):
            continue
        label = host if not port else host + ":" + port
        try:
            if opener is not None:
                opener(host, int(port) if port.isdigit() else 80)
            else:
                import socket as _socket
                with _socket.create_connection(
                        (host, int(port) if port.isdigit() else 80), timeout=3):
                    pass
            return None  # something answers; not our failure mode
        except Exception as exc:
            kind = type(exc).__name__ if type(exc).__name__.isalpha() else "Error"
            return "proxy " + label + " unreachable (" + kind[:32] + ")"
    return None


def gh_auth_finding(gh_bin: Optional[str] = None,
                    runner: Optional[Callable] = None) -> Optional[str]:
    """Bounded ``gh auth status`` verdict without leaking tokens or config.

    Returns None when gh is absent (git fallback still applies) or the login
    is valid; otherwise a fixed-shape one-line finding for the clone tail.
    """
    gh = gh_bin if gh_bin is not None else find_gh()
    if not gh:
        return None
    try:
        if runner is not None:
            proc = runner([gh, "auth", "status"], None, 15)
            code = proc.returncode
            detail = (proc.stderr or proc.stdout or "")
        else:
            import subprocess as _sp
            proc = _sp.run([gh, "auth", "status"], capture_output=True, text=True,
                           timeout=15, stdin=subprocess.DEVNULL, env=noninteractive_env())
            code = proc.returncode
            detail = (proc.stderr or proc.stdout or "")
    except Exception as exc:
        name = type(exc).__name__ if type(exc).__name__.isalpha() else "Error"
        return "gh auth probe failed (" + name[:32] + ")"
    if code == 0:
        return None
    first = ""
    for line in str(detail).splitlines():
        if line.strip():
            first = line.strip()[:160]
            break
    from .command_diagnostics import output_tail
    tail = output_tail(first)
    return "gh auth failed" + (": " + tail if tail else "")
