"""M15 host lifecycle guards (host-only, thin transport).

Single-authority split (M15-F6): the TypeScript server module
`src/session_lifecycle.ts` is the ONE canonical authority for session
classification (`classifySession`), grouped projection (`projectSessions`),
and cleanup planning (`planCleanup`). This Python host module owns ONLY:

- bounded opt-in text redaction (`sanitize_observation_text`),
- strict timezone-aware timestamp parsing (`_parse_ms`, no local/UTC guesses),
- fail-closed host target/execution guards (`validate_cleanup_target`),
- dedicated ownership-transfer fencing (`validate_ownership_transfer`).

It keeps NO second copy of server classification/planning rules and no
`classify_session` / `project_sessions` / `plan_cleanup` / DTO helpers.
Prior Python author classification tests migrated into the TS server suite
(`test/session-lifecycle-r2.test.ts`); see `docs/SESSION-LIFECYCLE.md`
"Test migration". Host strict validation here is independent execution-guard
validation, not a second registry and not a fake apply.

Stdlib only. No I/O, no effects. Real HTTP/SQLite auth and durable
decisions remain OPEN M15c; Git/worktree/process guards remain OPEN M15d.
"""

from __future__ import annotations

import re

SESSION_LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_lifecycle_observations(session_key TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_lifecycle_decisions(session_key TEXT NOT NULL, decision_id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, engine TEXT, target TEXT, created_at TEXT NOT NULL);
"""

_TZ_QUALIFIED = re.compile(r"T.*(?:Z|[+-]\d{2}:\d{2})$", re.IGNORECASE)
_SECRET_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
_SECRET_LABEL = re.compile(r"(token|secret|password|bearer)[^\n]{0,40}", re.IGNORECASE)
_SANITIZE_LOOKAHEAD = 128
_REDACTED = "[redacted]"


def sanitize_observation_text(raw, limit=512):
    """Backstop redaction for opt-in text only; reason codes are the default.

    Regex alone is never a privacy guarantee. Fix (F6): redact on a bounded
    window BEFORE final truncation and reject/drop oversize freetext rather
    than clipping inside a credential/`[redacted]` marker.
    """
    if raw is None or not isinstance(raw, str):
        return None
    if len(raw) > limit + _SANITIZE_LOOKAHEAD:
        return None
    window = raw[: limit + _SANITIZE_LOOKAHEAD]
    window = _SECRET_RUN.sub(_REDACTED, window)
    window = _SECRET_LABEL.sub(_REDACTED, window)
    text = window[:limit]
    last_open = text.rfind("[")
    if last_open != -1 and last_open > limit - len(_REDACTED) - 1:
        tail = text[last_open:]
        if tail != _REDACTED and _REDACTED.startswith(tail):
            text = text[:last_open]
    return text


def _parse_ms(value):
    """Strict TZ-aware parser (F4): explicit Z or ±HH:MM required.

    Timezone-naive / missing / unparseable returns None with no implicit
    local/UTC guess, so Python agrees with TS independent of OS local TZ.
    Named `limit_zone` never rescues a naive timestamp here (no audited DST
    resolution in this slice). Callers fail closed on present-but-None and
    on future values.
    """
    if not value or not isinstance(value, str):
        return None
    if not _TZ_QUALIFIED.search(value):
        return None
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp() * 1000.0
    except ValueError:
        return None


def _is_bad_owner(value):
    if not isinstance(value, str):
        return True
    t = value.strip().lower()
    return len(t) == 0 or t in ("unknown", "legacy")


def _is_valid_binding_id(value):
    if not isinstance(value, str):
        return False
    t = value.strip().lower()
    return len(t) > 0 and t not in ("unknown", "legacy")


def validate_cleanup_target(check):
    """Host validates the effect; server classifies the canonical record.

    F3: exact non-empty valid host/native/incarnation/owner binding required;
    blank/None/whitespace/unknown/legacy expected IDs or owner fail closed.
    """
    if not _is_valid_binding_id(check.get("expected_machine_id")) or not _is_valid_binding_id(
        check.get("expected_native_id")
    ):
        return {"ok": False, "error": "unbound_target"}
    if not _is_valid_binding_id(check.get("expected_owner")):
        return {"ok": False, "error": "unknown_owner_never_applies"}
    if check.get("observed_machine_id") != check.get("expected_machine_id"):
        return {"ok": False, "error": "machine_mismatch"}
    if check.get("observed_native_id") != check.get("expected_native_id"):
        return {"ok": False, "error": "incarnation_mismatch"}
    if check.get("observed_owner") != check.get("expected_owner"):
        return {"ok": False, "error": "custodian_changed"}
    if check.get("busy_or_draft_or_pending"):
        return {"ok": False, "error": "busy_draft_pending_guard"}
    if not check.get("contained"):
        return {"ok": False, "error": "containment_failed"}
    return {"ok": True}


def validate_ownership_transfer(request):
    """Dedicated transfer gate; generic intake text is never a transfer.

    F3: epochs must be non-bool safe ints >= 0 (`bool` is an `int` subclass
    in Python, so `True`/`False` must explicitly fail); negatives fail.
    """
    if request.get("human_owned"):
        return {"ok": False, "error": "human_protected"}
    if _is_bad_owner(request.get("new_owner")):
        return {"ok": False, "error": "unknown_owner_never_applies"}
    if _is_bad_owner(request.get("current_owner")) or _is_bad_owner(request.get("expected_owner")):
        return {"ok": False, "error": "unknown_owner_never_applies"}
    if request.get("expected_owner") != request.get("current_owner"):
        return {"ok": False, "error": "stale_owner"}
    exp = request.get("expected_epoch")
    cur = request.get("current_epoch")
    if (
        not isinstance(exp, int)
        or isinstance(exp, bool)
        or not isinstance(cur, int)
        or isinstance(cur, bool)
        or exp < 0
        or cur < 0
        or exp != cur
    ):
        return {"ok": False, "error": "epoch_fence_failed"}
    if not request.get("actor") or request.get("actor") != request.get("current_owner"):
        return {"ok": False, "error": "only_current_owner_transfers"}
    if request.get("new_owner") == request.get("current_owner"):
        return {"ok": False, "error": "no_op_transfer"}
    return {"ok": True}
