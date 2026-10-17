"""M15 host lifecycle guards + thin observation transport (M15c live).

Single-authority split (M15-F6): the TypeScript server module
`src/session_lifecycle.ts` is the ONE canonical authority for session
classification (`classifySession`), grouped projection (`projectSessions`),
and cleanup planning (`planCleanup`), and `src/session_lifecycle_registry.ts`
is the ONE durable authority for observations/decisions (DDL, joins, auth).
This Python host module owns ONLY:

- bounded opt-in text redaction (`sanitize_observation_text`),
- strict timezone-aware timestamp parsing (`_parse_ms`, no local/UTC guesses),
- fail-closed host target/execution guards (`validate_cleanup_target`),
- a NON-AUTHORITATIVE ownership-transfer pre-flight
  (`validate_ownership_transfer`: local syntax check only, the server
  decides; generic intake text is never a transfer),
- an allowlisted observation payload builder (`build_observation_payload`)
  for the `hub sessions observe` transport (owner/starter/human keys are
  dropped, never sent).

It keeps NO second copy of server classification/planning rules and no
`classify_session` / `project_sessions` / `plan_cleanup` / DTO helpers.
It keeps NO DDL copy: the server schema string in
`src/session_lifecycle.ts` is the single table authority (the former host
`SESSION_LIFECYCLE_SCHEMA` export is retired; the host never creates
lifecycle tables). Prior Python author classification tests migrated into
the TS server suite (`test/session-lifecycle-r2.test.ts`); see
`docs/SESSION-LIFECYCLE.md` "Test migration". Host strict validation here
is independent execution-guard validation, not a second registry and not
a fake apply.

Stdlib only. No I/O, no effects. Durable decisions live server-side;
Git/worktree/process guards remain OPEN M15d.
"""

from __future__ import annotations

import re

# NOTE: no SESSION_LIFECYCLE_SCHEMA here by design. Table DDL authority is
# the server string in src/session_lifecycle.ts (created inside
# SessionControlService.init); the host never creates lifecycle tables.

_TZ_QUALIFIED = re.compile(r"T.*(?:Z|[+-]\d{2}:\d{2})$", re.IGNORECASE)
_TRAILING_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})$")
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
    """Strict TZ-aware parser (F4, M15c-hardened): explicit Z or ±HH:MM required.

    Timezone-naive / missing / unparseable returns None with no implicit
    local/UTC guess, so Python agrees with TS independent of OS local TZ.
    Named `limit_zone` never rescues a naive timestamp here (no audited DST
    resolution in this slice). Callers fail closed on present-but-None and
    on future values.

    Consistency with the TS `ISO` parser (`src/session_lifecycle.ts`,
    same rule as `src/fleet.ts:40`): the trailing UTC offset range is
    checked explicitly (hours 00-23, minutes 00-59) so invalid offsets
    such as `+99:99` or `+24:00` return None on every Python version,
    exactly as `Date.parse` yields NaN (null) server-side.
    """
    if not value or not isinstance(value, str):
        return None
    if not _TZ_QUALIFIED.search(value):
        return None
    if not value[-1:].upper() == "Z":
        offset = _TRAILING_OFFSET.search(value)
        if not offset:
            return None
        try:
            hours = int(offset.group(2))
            minutes = int(offset.group(3))
        except ValueError:
            return None
        if hours > 23 or minutes > 59:
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
    """NON-AUTHORITATIVE pre-flight syntax check only (M15c).

    The server (`src/session_lifecycle_registry.ts`) is the sole transfer
    authority; this helper lets host-side tooling fail fast on malformed
    requests before transport. A True result here authorizes nothing.
    Generic intake text is never a transfer.

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


# Observation transport allowlist (M15c). Owner/starter/human/machine keys
# are deliberately absent: JSON never authenticates ownership, and the
# machine identity comes from the host credential, not the payload.
_OBSERVABLE_BOOL_FLAGS = (
    "awaiting_alexey",
    "limit_hit",
    "report_written",
    "screen_done",
    "authoritative_no_pending_proof",
    "pending_obligations",
    "has_active_children",
    "has_unsubmitted_draft",
    "has_unresolved_send_stop",
    "worktree_missing",
)
_OBSERVABLE_TEXT_FLAGS = (
    "awaiting_alexey_at",
    "limit_window",
    "limit_reset_at",
    "limit_zone",
    "report_at",
    "terminal_evidence",
    "terminal_evidence_at",
    "superseded_by",
)
_DROPPED_IDENTITY_KEYS = (
    "owner",
    "starter",
    "owner_provenance",
    "human_owned",
    "machine_id",
    "native_id",
    "engine",
    "actor",
)


def build_observation_payload(fields):
    """Build the `payload` dict for `hub sessions observe` transport.

    Allowlisted lifecycle flags only; owner/starter/human/machine identity
    keys are silently dropped (never sent, never trusted). Free-text notes
    are opt-in, bounded, and redacted via `sanitize_observation_text`.
    Raises ValueError on mistyped/unknown fields (fail closed, no guess).
    """
    if not isinstance(fields, dict):
        raise ValueError("Observation fields must be a mapping")
    payload = {}
    for key, value in fields.items():
        if key in _DROPPED_IDENTITY_KEYS:
            continue
        if key in _OBSERVABLE_BOOL_FLAGS:
            if not isinstance(value, bool):
                raise ValueError("Observation flag %r must be a boolean" % (key,))
            payload[key] = value
        elif key in _OBSERVABLE_TEXT_FLAGS:
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError("Observation field %r must be a non-empty string <=512" % (key,))
            if key == "terminal_evidence" and value not in ("explicit_completion", "supersession_record"):
                raise ValueError("Unknown terminal_evidence value")
            payload[key] = value
        elif key == "observation_note":
            if not isinstance(value, str):
                raise ValueError("observation_note must be a string")
            note = sanitize_observation_text(value, 512)
            if note is None:
                raise ValueError("observation_note oversize or unredactable; omitted")
            payload["observation_note"] = note
            payload["note_text_opt_in"] = True
        else:
            raise ValueError("Unknown observation field %r" % (key,))
    return payload
