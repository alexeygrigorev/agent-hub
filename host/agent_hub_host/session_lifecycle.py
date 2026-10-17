"""M15 session lifecycle registry (projection + plan seam, head-corrected). Stdlib only.

Mirrors src/session_lifecycle.ts. Pure functions over caller-supplied snapshots;
no session identity/provenance/control duplication, no native effects, no I/O.
Head-ACKed direction (not wired here): additive GET /api/sessions?view=lifecycle,
POST /api/sessions/cleanup (dry-run default), existing POST decision seam;
CLI hub sessions / hub sessions cleanup preserving list/start/send/capture/stop.
Durable decisions, server CLI/apply executor and host acceptance are OPEN M15c/d.
Default projection carries reason codes only; free text is opt-in and redacted.
"""

from __future__ import annotations

SESSION_LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_lifecycle_observations(session_key TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_lifecycle_decisions(session_key TEXT NOT NULL, decision_id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, engine TEXT, target TEXT, created_at TEXT NOT NULL);
"""

GROUP_ORDER = ("waiting_alexey", "limit_blocked", "finished_stale", "active", "unknown")

import re
import time
from datetime import datetime, timezone

_SECRET_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
_SECRET_LABEL = re.compile(r"(token|secret|password|bearer)[^\n]{0,40}", re.IGNORECASE)


def sanitize_observation_text(raw, limit=512):
    """Backstop redaction for opt-in text only; reason codes are the default."""
    if raw is None or not isinstance(raw, str):
        return None
    text = raw[:limit]
    text = _SECRET_RUN.sub("[redacted]", text)
    text = _SECRET_LABEL.sub("[redacted]", text)
    return text


def _parse_ms(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() * 1000.0
    except ValueError:
        return None


def session_key(item):
    return "%s/%s" % (item.get("machine_id"), item.get("native_id"))


def _flags(item, now):
    flags, obligations = [], []
    if item.get("worktree_missing"):
        flags.append("missing_worktree")
    if item.get("awaiting_alexey"):
        at = _parse_ms(item.get("awaiting_alexey_at"))
        flags.append("waiting_alexey_fresh" if at is not None and now >= at and now - at <= 24 * 3600 * 1000
                     else "waiting_alexey_stale")
    if item.get("limit_hit"):
        flags.append("limit_known_reset" if _parse_ms(item.get("limit_reset_at")) is not None
                     else "limit_unknown_reset")
    if item.get("pending_obligations"):
        obligations.append("pending_obligations")
    if item.get("has_active_children"):
        obligations.append("active_children")
    if item.get("has_unsubmitted_draft"):
        obligations.append("unsubmitted_draft")
    if item.get("has_unresolved_send_stop"):
        obligations.append("unresolved_send_stop")
    if item.get("report_written"):
        flags.append("report_present")
    if item.get("screen_done"):
        flags.append("screen_done")
    if item.get("terminal_evidence"):
        flags.append("terminal_%s" % item.get("terminal_evidence"))
    if item.get("superseded_by"):
        flags.append("superseded_marker")
    return flags, obligations


def classify_session(item, idle_hours=24, now_ms=None):
    now = now_ms if now_ms is not None else time.time() * 1000.0
    key = session_key(item)
    reasons = []
    flags, obligations = _flags(item, now)
    note = sanitize_observation_text(item.get("observation_note")) if item.get("note_text_opt_in") else None

    if item.get("worktree_missing"):
        reasons.append("worktree record present but host reports workspace absent")
        return {"session_key": key, "group": "unknown", "state": "missing_worktree_unknown",
                "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}
    if item.get("awaiting_alexey"):
        at = _parse_ms(item.get("awaiting_alexey_at"))
        if at is not None and now >= at and now - at <= 24 * 3600 * 1000:
            reasons.append("fresh question/approval pending for Alexey")
            return {"session_key": key, "group": "waiting_alexey", "state": "waiting_alexey",
                    "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}
        reasons.append("stale awaiting-Alexey evidence ignored")
    if item.get("limit_hit"):
        window = item.get("limit_window") or "unknown"
        reset_known = _parse_ms(item.get("limit_reset_at")) is not None
        reasons.append("limit pattern matched window=%s reset=%s; unknown stays needs-action, never resumes from timestamp alone"
                       % (window, "known" if reset_known else "unknown"))
        return {"session_key": key, "group": "limit_blocked",
                "state": "limit_blocked" if reset_known else "limit_blocked_unknown_reset",
                "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}
    no_obligations = (not item.get("pending_obligations") and not item.get("has_active_children")
                      and not item.get("has_unsubmitted_draft") and not item.get("has_unresolved_send_stop"))
    activity = _parse_ms(item.get("last_activity_at"))
    report = _parse_ms(item.get("report_at"))
    terminal_at = _parse_ms(item.get("terminal_evidence_at"))
    terminal_fresh = (item.get("terminal_evidence") is not None and terminal_at is not None
                      and now >= terminal_at and now - terminal_at <= 24 * 3600 * 1000)
    report_fresh = report is not None and (activity is None or activity <= report)
    finished_signals = bool(item.get("report_written") and item.get("screen_done"))
    authoritative = bool(item.get("authoritative_no_pending_proof"))
    if item.get("superseded_by") and no_obligations and (finished_signals or (terminal_fresh and authoritative)):
        reasons.append("superseded with authoritative no-pending proof")
        return {"session_key": key, "group": "finished_stale", "state": "superseded",
                "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}
    if item.get("superseded_by") and not no_obligations:
        reasons.append("superseded marker present but obligations remain")
    if no_obligations and authoritative and (finished_signals or terminal_fresh) and (report_fresh or terminal_fresh):
        reasons.append("terminal completion with authoritative no-pending proof and no newer activity")
        return {"session_key": key, "group": "finished_stale", "state": "finished",
                "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}
    if (item.get("report_written") or item.get("screen_done") or item.get("terminal_evidence")) and (
            not no_obligations or (not report_fresh and not terminal_fresh)):
        reasons.append("report/screen/terminal alone never qualifies without authoritative no-pending proof")
    idle = None if activity is None else (now - activity) / 3600000.0
    if idle is not None and idle >= idle_hours:
        reasons.append("idle %.1fh >= %sh threshold" % (idle, idle_hours))
        return {"session_key": key, "group": "active", "state": "idle",
                "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}
    phase = str(item.get("phase") or "").lower()
    if phase == "running" or idle is None or idle < idle_hours:
        if idle is None:
            reasons.append("no activity timestamp; treating as unknown-fresh active")
        else:
            reasons.append("fresh activity within idle threshold")
        return {"session_key": key, "group": "active", "state": phase or "active",
                "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}
    reasons.append("insufficient fresh provenance")
    return {"session_key": key, "group": "unknown", "state": "unknown",
            "reasons": reasons, "flags": flags, "obligations": obligations, "sanitized_note": note}


def project_sessions(inputs, owner=None, task=None, host=None, idle_hours=24, now_ms=None,
                     limit=50, cursor=None):
    limit = min(max(limit, 1), 50)
    scoped = [s for s in inputs
              if (host is None or s.get("machine_id") == host)
              and (owner is None or (s.get("owner")) == owner)
              and (task is None or task in (s.get("task_refs") or []))]
    classified = [classify_session(s, idle_hours=idle_hours, now_ms=now_ms) for s in scoped]
    classified.sort(key=lambda c: (GROUP_ORDER.index(c["group"]), c["session_key"]))
    start = 0
    if cursor:
        positions = [i for i, c in enumerate(classified) if c["session_key"] == cursor]
        start = positions[0] + 1 if positions else 0
    page = classified[start:start + limit]
    groups = {name: [] for name in GROUP_ORDER}
    for entry in page:
        groups[entry["group"]].append(entry)
    next_cursor = page[-1]["session_key"] if start + limit < len(classified) else None
    return {"groups": groups, "next_cursor": next_cursor, "total": len(classified)}


def plan_cleanup(candidates, caller_owner):
    """Pure dry-run plan only; durable decisions/apply/host acceptance are OPEN M15c/d."""
    eligible, ineligible = [], []
    for cand in candidates:
        blockers = []
        if cand.get("human_owned"):
            blockers.append("human/Alexey session permanently protected")
        if (cand.get("owner")) != caller_owner:
            blockers.append("not owned by caller (admin is not ownership)")
        if (cand.get("owner") or None) in (None, "unknown", "legacy"):
            blockers.append("unknown/legacy owner cannot authorize cleanup")
        if cand.get("group") != "finished_stale":
            blockers.append("group %s never eligible (idle/limit/waiting alone excluded)" % cand.get("group"))
        if cand.get("pending_obligations") or cand.get("obligations"):
            blockers.append("pending obligations remain")
        if cand.get("state") not in ("finished", "superseded"):
            blockers.append("state %s not confirmed finished/superseded" % cand.get("state"))
        if blockers:
            ineligible.append({"key": cand.get("session_key"), "reasons": blockers})
        else:
            eligible.append(cand)
    return {"eligible": eligible, "ineligible": ineligible}


def validate_cleanup_target(check):
    """Host validates the effect; server classifies the canonical record."""
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
    """Dedicated transfer gate; generic intake text is never a transfer."""
    if request.get("human_owned"):
        return {"ok": False, "error": "human_protected"}
    if (request.get("new_owner") or None) in (None, "unknown", "legacy", ""):
        return {"ok": False, "error": "unknown_owner_never_applies"}
    if request.get("expected_owner") != request.get("current_owner"):
        return {"ok": False, "error": "stale_owner"}
    if (not isinstance(request.get("expected_epoch"), int)
            or not isinstance(request.get("current_epoch"), int)
            or request.get("expected_epoch") != request.get("current_epoch")):
        return {"ok": False, "error": "epoch_fence_failed"}
    if not request.get("actor") or request.get("actor") != request.get("current_owner"):
        return {"ok": False, "error": "only_current_owner_transfers"}
    if request.get("new_owner") == request.get("current_owner"):
        return {"ok": False, "error": "no_op_transfer"}
    return {"ok": True}


def to_status_dto(classified):
    return {"session_key": classified["session_key"], "group": classified["group"],
            "state": classified["state"], "reasons": classified["reasons"],
            "flags": classified["flags"], "obligations": classified["obligations"],
            "note": classified["sanitized_note"]}


def to_handoff_dto(classified):
    dto = to_status_dto(classified)
    dto["text_as_data"] = True
    return dto
