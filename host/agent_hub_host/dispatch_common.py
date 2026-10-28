"""Helpers shared by the root and role dispatchers."""
from __future__ import annotations

import os
import pathlib
from typing import Any, Optional


def scrub(text: Any, *secrets: Optional[str]) -> str:
    out = str(text)
    for s in secrets:
        if isinstance(s, str) and s:
            out = out.replace(s, "[redacted]")
    return out


def _resolve(path: str | os.PathLike) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(str(path))).resolve()


def expand_trusted_path(raw: str | os.PathLike) -> str:
    """Expand a configured path: absolute, or ``~`` / ``~/...`` (never ``~user``)."""
    s = str(raw)
    if s == "~" or s.startswith("~/"):
        s = os.path.expanduser(s)
    return s
