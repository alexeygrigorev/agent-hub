"""M15 retired-native session verification, tombstone validation, and authentic history preservation.

Read-only native state inspection:
- Reads ONLY trusted explicit paths derived from validated native session UUIDs:
  `<state_dir>/retired-sessions/<norm_uuid>/tombstone.json`
  `<state_dir>/retired-sessions/<norm_uuid>/history.bin`
  `<state_dir>/sessions/<norm_uuid>/history.bin`
  `<state_dir>/retired-sessions/<norm_uuid>/screen.txt`
  `<state_dir>/sessions/<norm_uuid>/screen.txt`
- Never performs directory walks, globbing, or history scans.
- Never accesses network-supplied or caller-crafted paths.
- Enforces regular non-symlink (nofollow) files with bounded sizes.
- Missing live record alone or exitcode 0 is NEVER exit proof.
- Bounded tail of authentic retired history is preserved with private 0600 and fsync.
- If authentic retired history is unavailable, the worktree is kept with precise reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
import uuid

from . import private_fs
from .native_bridge import canonicalize_workspace
from .native_sessions import (
    TERMINAL_PHASES,
    NativeSession,
    NativeTarget,
    SessionControlError,
    SessionUncertain,
    list_sessions,
)

MAX_TOMBSTONE_BYTES = 4096
DEFAULT_HISTORY_TAIL_BYTES = 64 * 1024


def resolve_aplexer_state_dir(state_dir: Optional[str] = None) -> str:
    """Resolve trusted aplexer state directory.

    Order of resolution:
    1. Explicit non-empty `state_dir` argument (str or path-like).
    2. `APLEXER_STATE_DIR` environment variable.
    3. `$XDG_STATE_HOME/aplexer` if `XDG_STATE_HOME` is set.
    4. Default: `~/.local/state/aplexer`.
    """
    if state_dir is not None:
        # F5: accept path-like (tests pass Path); str() first so explicit
        # Path inputs resolve exactly like the old Path(...) inline logic.
        text = os.fspath(state_dir) if isinstance(state_dir, os.PathLike) else state_dir
        if isinstance(text, str) and text.strip():
            candidate = os.path.abspath(text.strip())
            return canonicalize_workspace(candidate)

    env_state = os.environ.get("APLEXER_STATE_DIR")
    if env_state and env_state.strip():
        return canonicalize_workspace(os.path.abspath(env_state.strip()))

    env_xdg = os.environ.get("XDG_STATE_HOME")
    if env_xdg and env_xdg.strip():
        return canonicalize_workspace(os.path.abspath(os.path.join(env_xdg.strip(), "aplexer")))

    return canonicalize_workspace(os.path.expanduser("~/.local/state/aplexer"))


def validate_retired_tombstone(
    tombstone_path: str,
    target: NativeTarget,
) -> dict[str, Any]:
    """Validate a genuine retired native tombstone against target expectations.

    Contract requirements:
    - Path must be an existing regular file; symlinks are strictly rejected (nofollow).
    - Size must be non-empty and bounded (<= 4 KiB).
    - Valid JSON object.
    - `cause` must equal "finished" (clean exit / accepted completion; forgotten/pruned refuse).
    - `id` must match target.session_id (case-insensitive normalized UUID).
    - `tag` must match target.tag exactly.
    - `workspace` canonical realpath must match target.workspace canonical realpath.
    - `finished_at_ms` must be a non-negative integer (fractions/floats/booleans rejected).
      If target carries created_at_ms, finished_at_ms must be >= target.created_at_ms.
    """
    target = target.validated()
    if not isinstance(tombstone_path, str) or not tombstone_path:
        raise SessionControlError("tombstone_path is required")

    if os.path.islink(tombstone_path):
        raise SessionControlError(f"tombstone file is a symlink (unsafe): {tombstone_path!r}")

    if not os.path.isfile(tombstone_path):
        raise SessionControlError(f"tombstone file does not exist: {tombstone_path!r}")

    try:
        st = os.stat(tombstone_path)
    except OSError as exc:
        raise SessionControlError(f"Cannot stat tombstone file: {exc}") from exc

    if st.st_size <= 0:
        raise SessionControlError(f"tombstone file is empty: {tombstone_path!r}")
    if st.st_size > MAX_TOMBSTONE_BYTES:
        raise SessionControlError(f"tombstone file exceeds size bound ({st.st_size} > {MAX_TOMBSTONE_BYTES})")

    try:
        raw = private_fs.read_bytes(tombstone_path, MAX_TOMBSTONE_BYTES, private=False).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionControlError("Cannot decode tombstone file as UTF-8") from exc
    except ValueError as exc:
        raise SessionControlError("tombstone file exceeds size bound during read") from exc
    except OSError as exc:
        raise SessionControlError(f"Cannot read tombstone file: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SessionControlError(f"tombstone file contains invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionControlError("tombstone JSON is not an object")

    # 1. Cause check: only "finished" proves natural completion
    cause = data.get("cause")
    if cause != "finished":
        raise SessionControlError(f"Tombstone cause is {cause!r}, expected 'finished'")

    # 2. Session ID check
    raw_id = data.get("id")
    try:
        norm_tombstone_id = str(uuid.UUID(str(raw_id))).lower()
    except (ValueError, TypeError) as exc:
        raise SessionControlError(f"Tombstone carries invalid session UUID {raw_id!r}") from exc
    if norm_tombstone_id != target.session_id.lower():
        raise SessionControlError(
            f"Tombstone session ID mismatch (tombstone: {norm_tombstone_id}, target: {target.session_id})"
        )

    # 3. Tag check
    tombstone_tag = data.get("tag")
    if not isinstance(tombstone_tag, str) or tombstone_tag != target.tag:
        raise SessionControlError(
            f"Tombstone tag mismatch (tombstone: {tombstone_tag!r}, target: {target.tag!r})"
        )

    # 4. Workspace check
    raw_ws = data.get("workspace")
    if not isinstance(raw_ws, str) or not raw_ws:
        raise SessionControlError("Tombstone missing workspace path")
    # Linux /proc/<pid>/cwd appends this marker after an unlinked cwd.
    # It describes the original workspace, not a directory named " (deleted)".
    if raw_ws.endswith(" (deleted)"):
        raw_ws = raw_ws[:-10]
    canonical_tombstone_ws = canonicalize_workspace(raw_ws)
    if canonical_tombstone_ws != target.workspace:
        raise SessionControlError(
            f"Tombstone workspace mismatch (tombstone: {canonical_tombstone_ws!r}, target: {target.workspace!r})"
        )

    # 5. Finished timestamp check
    finished_at_ms = data.get("finished_at_ms")
    # Python bool is a subclass of int, so reject booleans explicitly
    if isinstance(finished_at_ms, bool) or not isinstance(finished_at_ms, int) or type(finished_at_ms) is not int:
        raise SessionControlError(f"Tombstone finished_at_ms must be an integer, got {type(finished_at_ms).__name__}")
    if finished_at_ms < 0:
        raise SessionControlError(f"Tombstone finished_at_ms must be non-negative, got {finished_at_ms}")

    if target.created_at_ms is not None and finished_at_ms < target.created_at_ms:
        raise SessionControlError(
            f"Tombstone finished_at_ms ({finished_at_ms}) predates target created_at_ms ({target.created_at_ms})"
        )

    return data


def verify_native_retirement(
    executable: str,
    target: NativeTarget,
    state_dir: Optional[str] = None,
) -> tuple[bool, Optional[dict[str, Any]], str]:
    """Verify natural retirement of an owned native session.

    Returns: `(is_retired, tombstone_dict_or_none, detail_reason)`

    Conditions required for retirement proof:
    1. Valid, non-failing live catalog query: if `list_sessions` errors, retirement cannot be proven (fail closed).
    2. Target session UUID must be completely ABSENT from all catalog rows.
    3. No conflicting non-terminal session exists on the target's tag in the target workspace (tag reuse protection).
    4. Bounded, regular (nofollow) tombstone file exists at `<state_dir>/retired-sessions/<norm_uuid>/tombstone.json`.
    5. Tombstone passes all validation checks in `validate_retired_tombstone`.
    """
    try:
        target = target.validated()
    except SessionControlError as exc:
        return False, None, f"invalid_target: {exc}"

    try:
        sessions = list_sessions(executable, include_exited=True)
    except (SessionControlError, SessionUncertain) as exc:
        return False, None, f"live_catalog_query_failed: {exc}"
    except Exception as exc:
        return False, None, f"live_catalog_query_error_{type(exc).__name__}: {exc}"

    # Target UUID must NOT appear in live catalog
    for s in sessions:
        if s.session_id.lower() == target.session_id.lower():
            if s.phase not in TERMINAL_PHASES:
                return False, None, f"target_still_active_in_catalog: phase={s.phase}"
            return False, None, f"target_still_present_in_catalog: phase={s.phase}"

    # Tag must not be held by another active session in the same workspace
    for s in sessions:
        if s.tag == target.tag and canonicalize_workspace(s.workspace) == target.workspace:
            if s.phase not in TERMINAL_PHASES:
                return False, None, f"tag_reused_by_active_session: session_id={s.session_id} phase={s.phase}"

    # Locate trusted tombstone file
    resolved_state = resolve_aplexer_state_dir(state_dir)
    norm_uuid = str(uuid.UUID(target.session_id)).lower()
    tombstone_path = os.path.join(resolved_state, "retired-sessions", norm_uuid, "tombstone.json")

    if not os.path.exists(tombstone_path):
        return False, None, f"retired_tombstone_absent: {tombstone_path}"

    try:
        tombstone = validate_retired_tombstone(tombstone_path, target)
    except SessionControlError as exc:
        return False, None, f"retired_tombstone_invalid: {exc}"

    return True, tombstone, "retired_tombstone_verified"


def capture_retired_history(
    target: NativeTarget,
    state_dir: Optional[str] = None,
    limit_bytes: int = DEFAULT_HISTORY_TAIL_BYTES,
) -> tuple[Optional[bytes], Optional[str], Optional[str], str]:
    """Capture a bounded tail of genuine retired native history or terminal screen snapshot.

    Returns: `(raw_bytes, source_path, mode, status_message)`

    Searches explicit trusted paths in priority order:
    1. `<state_dir>/retired-sessions/<norm_uuid>/history.bin` (tail)
    2. `<state_dir>/sessions/<norm_uuid>/history.bin` (tail)
    3. `<state_dir>/retired-sessions/<norm_uuid>/screen.txt` (screen)
    4. `<state_dir>/sessions/<norm_uuid>/screen.txt` (screen)

    Guards:
    - Never follows symlinks.
    - Skips non-existent or 0-byte files (no fake/empty captures).
    - Bounds tail to `limit_bytes`.
    - If none available, returns `(None, None, None, "authentic retired history unavailable; listed-only")`.
    """
    try:
        target = target.validated()
    except SessionControlError as exc:
        return None, None, None, f"invalid_target: {exc}"

    if type(limit_bytes) is not int or not 1 <= limit_bytes <= DEFAULT_HISTORY_TAIL_BYTES:
        raise SessionControlError("History tail bound must be an integer in 1..65536")
    resolved_state = resolve_aplexer_state_dir(state_dir)
    norm_uuid = str(uuid.UUID(target.session_id)).lower()

    candidates = [
        (os.path.join(resolved_state, "retired-sessions", norm_uuid, "history.bin"), "tail"),
        (os.path.join(resolved_state, "sessions", norm_uuid, "history.bin"), "tail"),
        (os.path.join(resolved_state, "retired-sessions", norm_uuid, "screen.txt"), "screen"),
        (os.path.join(resolved_state, "sessions", norm_uuid, "screen.txt"), "screen"),
    ]

    for path, mode in candidates:
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with private_fs.parent(path, private=False) as anchor:
                fd = private_fs._open(anchor, path, os.O_RDONLY, private=False)
                with os.fdopen(fd, "rb") as f:
                    size = os.fstat(f.fileno()).st_size
                    if size <= 0:
                        continue
                    f.seek(max(0, size - limit_bytes))
                    data = f.read(limit_bytes)
            if data:
                return data, path, mode, "authentic_history_captured"
        except OSError:
            continue

    return None, None, None, "authentic retired history unavailable; listed-only"
