"""Shared helpers used by the bounded host monitoring producers."""

from __future__ import annotations

import os
from datetime import datetime, timezone


def resolve_config_path(
    explicit: str | None,
    *,
    env_var: str,
    filename: str,
) -> str:
    """Resolve a trusted monitor config path with explicit and env priority."""
    if explicit:
        return explicit
    configured = os.environ.get(env_var)
    if configured:
        return configured
    cwd = os.getcwd()
    candidates = (
        os.path.join(cwd, ".local", "session", filename),
        os.path.join(cwd, "journal", filename),
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def utc_now_iso() -> str:
    """Return the monitoring timestamp format used by each producer."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_origin_and_token(
    base_url: str | None,
    token: str | None,
    *,
    missing_token_message: str,
) -> tuple[str, str]:
    """Resolve Hub origin and host token with the shared precedence rules."""
    from . import api, auth, config

    settings = config.load_config()
    origin = (
        base_url
        or os.environ.get("HUB_URL")
        or os.environ.get("HUB_BASE_URL")
        or settings.get("base_url")
        or config.DEFAULT_BASE_URL
    )
    api.check_url(origin)
    env_token = os.environ.get("HUB_TOKEN") or os.environ.get("HUB_JOB_TOKEN")
    access_token = token or env_token or auth.get_access_token()
    if not access_token:
        raise ValueError(missing_token_message)
    return origin.rstrip("/"), access_token
