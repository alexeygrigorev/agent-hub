"""Native aplexer work-claim bridge (Phase 1, minimal v1).

Observes local-first worker state and mirrors redacted summaries into Hub so
operators see who is already working and can skip duplicates:

- live declarations: per-session ``coordination.json`` (``a work join``),
  correlated across sibling worktrees via ``git_common_dir``;
- durable lifecycle events: ``kind LIKE 'work.%'`` envelopes
  (``work.claim|complete|abandon|steal|handoff`` with ``hub.work-claim.v1``
  data) from canonical workspace mailboxes — including mailboxes of
  temporary worktrees whose workspace shares the canonical ``git_common_dir``
  (the worktree mailbox trap: sender sessions always write to their own
  record workspace, never to the canonical path).

Everything crossing into Hub is allowlist-validated and secret-scrubbed
(design section 7.10) before the POST to ``/api/bus/native-sync``. Sync is
best-effort and never fails the host poll cycle; the server treats synced
rows as host-attested observations (idempotent upserts, TTL expiry reaps
ghosts, exactly like Hub-native claims).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

WORK_CLAIM_SCHEMA = "hub.work-claim.v1"

WORK_MESSAGE_KINDS = (
    "work.claim",
    "work.heartbeat",
    "work.complete",
    "work.abandon",
    "work.steal",
    "work.handoff",
)

WORK_CLAIM_EVENTS = ("claim", "heartbeat", "complete", "abandon", "steal", "handoff")
TERMINAL_EVENTS = ("complete", "abandon")

DEFAULT_LEASE_SEC = 1800
LEASE_GRACE_SEC = 120
MAX_ITEMS_PER_SYNC = 64
MAX_MAILBOXES_PER_SCAN = 200
MAX_MSGS_PER_MAILBOX = 500

_CLAIM_ID_RE = re.compile(r"\Aclm_[0-9A-HJKMNP-TV-Z]{10,26}\Z")
_WORK_KEY_RE = re.compile(r"\A(task|intake|ci|pr|issue|path):[A-Za-z0-9._:/-]{1,250}\Z")
_ALIAS_KEY_RE = re.compile(r"\A(task|intake|ci|pr|issue|path|mail):[A-Za-z0-9._:/-]{1,250}\Z")
_PROJECT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ACTOR_SEG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MACHINE_SEG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_COMMIT_SHA_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")
_COMMIT_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_TASK_HEAD_RE = re.compile(
    r"\A(?P<work_key>\S+)(?P<brackets>(?:\s+\[[^\]]+\])*)\s*(?:\|\s*(?P<summary>.*))?\Z"
)
_BRACKET_CLAIM_RE = re.compile(r"\[claim=([^\]]+)\]")
_BRACKET_TTL_RE = re.compile(r"\[ttl=(\d+)s\]")

# Secret redaction (design 7.10). Same vectors as src/work_claim.ts.
_TOKEN_RE = re.compile(
    r"(hubt_[A-Za-z0-9_-]+|hubr_[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|"
    r"xox[baprs]-[A-Za-z0-9-]+)"
)
# Value stops at `&`/`#` as well as whitespace so URL neighbors survive;
# the URL pass runs first and this stays idempotent on `[REDACTED]`.
_BEARER_ENV_RE = re.compile(
    r"(authorization\s*[:=]\s*bearer\s+|token\s*[:=]\s*|password\s*[:=]\s*|"
    r"secret\s*[:=]\s*|api[_-]?key\s*[:=]\s*)([^\s\"',;&#]+)",
    re.IGNORECASE,
)
_AWS_KEY_RE = re.compile(r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL_USERINFO_RE = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_URL_QUERY_RE = re.compile(
    r"([?&](?:token|key|sig|signature|secret|auth|password|code|state|x-amz-[^=&#\s]+)=)[^&#\s]*",
    re.IGNORECASE,
)
# NOTE: `/` and `=` are deliberately excluded from the run: paths and
# `key=value` pairs would otherwise glue into false positives (URL query
# secrets are handled by _URL_QUERY_RE; padding `=` never starts a token).
_HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9+_-]{40,}")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Host-synthesized ids must satisfy the design claim_id pattern
# ^clm_[0-9A-HJKMNP-TV-Z]{10,26}$ (no I, L, O, Q, R, S, U, dash or underscore).
_HOST_CLAIM_ALPHABET = "0123456789ABCDEFGHJKMNPTVWXYZ"


def redact_for_bus(value: Any) -> Any:
    """Scrub one free-text string per design 7.10. Non-strings pass through."""
    if not isinstance(value, str) or not value:
        return value
    out = _PEM_RE.sub("[REDACTED_PRIVATE_KEY]", value)
    out = _TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _URL_USERINFO_RE.sub(r"\1", out)
    out = _URL_QUERY_RE.sub(r"\1[REDACTED]", out)
    out = _BEARER_ENV_RE.sub(r"\1[REDACTED]", out)
    out = _AWS_KEY_RE.sub("[REDACTED_AWS_KEY]", out)

    def _entropy(match: "re.Match[str]") -> str:
        token = match.group(0)
        # Recognized non-secret identifiers survive the heuristic: a
        # lowercase hex commit SHA, or a pure digit run (counts, timestamps).
        if _COMMIT_SHA40_RE.match(token):
            return token
        if token.isdigit():
            return token
        return "[REDACTED_HIGH_ENTROPY]"

    return _HIGH_ENTROPY_RE.sub(_entropy, out)


def redact_claim_data(data: Any) -> Any:
    """Deep-redact every string value inside a validated claim payload."""
    if isinstance(data, str):
        return redact_for_bus(data)
    if isinstance(data, list):
        return [redact_claim_data(v) for v in data]
    if isinstance(data, dict):
        return {k: redact_claim_data(v) for k, v in data.items()}
    return data


def parse_work_task_string(task: Any) -> dict[str, Any]:
    """Parse an ``a work join --task`` string per the design 9.1 convention.

    ``<work_key> [hub_task=<uuid>] [claim=<claim_id>] [ttl=<sec>s] | <summary>``
    """
    if not isinstance(task, str) or not task:
        return {"work_key": None, "claim_id": None, "ttl_sec": None, "summary": ""}
    pipe = task.find("|")
    head = (task[:pipe] if pipe >= 0 else task).strip()
    summary = (task[pipe + 1:] if pipe >= 0 else "").strip()[:280]
    match = _TASK_HEAD_RE.match(head)
    if not match:
        return {"work_key": None, "claim_id": None, "ttl_sec": None, "summary": summary}
    first = (match.group("work_key") or "").strip()
    work_key = first if _WORK_KEY_RE.match(first) else None
    brackets = match.group("brackets") or ""
    claim_id: Optional[str] = None
    ttl_sec: Optional[int] = None
    claim_match = _BRACKET_CLAIM_RE.search(brackets)
    if claim_match and _CLAIM_ID_RE.match(claim_match.group(1)):
        claim_id = claim_match.group(1)
    ttl_match = _BRACKET_TTL_RE.search(brackets)
    if ttl_match:
        try:
            candidate = int(ttl_match.group(1))
        except ValueError:
            candidate = 0
        if 60 <= candidate <= 14400:
            ttl_sec = candidate
    return {"work_key": work_key, "claim_id": claim_id, "ttl_sec": ttl_sec, "summary": summary}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_len(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def validate_work_claim_data(data: Any) -> Optional[str]:
    """Strict validation of a ``hub.work-claim.v1`` payload (design 9.2).

    Returns None when valid, otherwise a short machine-readable reason.
    Unknown top-level fields are forbidden (schema allowlist).
    """
    if not isinstance(data, dict):
        return "payload must be an object"
    allowed = {
        "schema", "event", "claim_id", "seq", "work_key", "alias_keys",
        "project", "workspace", "worktree", "mode", "scopes", "holder",
        "lineage", "lease_ttl_sec", "outcome", "summary",
    }
    for key in data:
        if key not in allowed:
            return f"forbidden field {key}"
    if data.get("schema") != WORK_CLAIM_SCHEMA:
        return "schema must be hub.work-claim.v1"
    if data.get("event") not in WORK_CLAIM_EVENTS:
        return "event invalid"
    if not isinstance(data.get("claim_id"), str) or not _CLAIM_ID_RE.match(data["claim_id"]):
        return "claim_id invalid"
    if not _is_int(data.get("seq")) or data["seq"] < 1:
        return "seq invalid"
    if not isinstance(data.get("work_key"), str) or not _WORK_KEY_RE.match(data["work_key"]):
        return "work_key invalid"
    aliases = data.get("alias_keys")
    if aliases is not None:
        if not isinstance(aliases, list) or len(aliases) > 16:
            return "alias_keys invalid"
        for entry in aliases:
            if not isinstance(entry, str) or not _ALIAS_KEY_RE.match(entry):
                return "alias_keys entry invalid"
    if not _str_len(data.get("project"), 64) or not _PROJECT_RE.match(data["project"]):
        return "project invalid"
    workspace = data.get("workspace")
    if not isinstance(workspace, str) or not workspace or len(workspace) > 1024:
        return "workspace invalid"
    worktree = data.get("worktree")
    if worktree is not None and (not isinstance(worktree, str) or not worktree or len(worktree) > 1024):
        return "worktree invalid"
    if data.get("mode") not in ("edit", "read", "review", "coord"):
        return "mode invalid"
    scopes = data.get("scopes")
    if scopes is not None:
        if not isinstance(scopes, list) or len(scopes) > 64:
            return "scopes invalid"
        for entry in scopes:
            if not isinstance(entry, str) or not entry or len(entry) > 512:
                return "scope entry invalid"
            if ".." in entry or "\\" in entry or entry.startswith("/"):
                return "scope entry not workspace-relative"
    holder = data.get("holder")
    if not isinstance(holder, dict):
        return "holder must be an object"
    for key in holder:
        if key not in {"machine_id", "session_id", "tag", "engine", "hub_agent", "hub_session_id"}:
            return f"holder has forbidden field {key}"
    if not _str_len(holder.get("machine_id"), 64):
        return "holder.machine_id invalid"
    if not _str_len(holder.get("session_id"), 64):
        return "holder.session_id invalid"
    if not _str_len(holder.get("tag"), 128):
        return "holder.tag invalid"
    if holder.get("engine") is not None and not _str_len(holder.get("engine"), 64):
        return "holder.engine invalid"
    if holder.get("hub_agent") is not None and not _str_len(holder.get("hub_agent"), 128):
        return "holder.hub_agent invalid"
    if holder.get("hub_session_id") is not None and not _str_len(holder.get("hub_session_id"), 64):
        return "holder.hub_session_id invalid"
    lineage = data.get("lineage")
    if lineage is not None:
        if not isinstance(lineage, dict):
            return "lineage must be an object"
        for key in lineage:
            if key not in {"task_id", "parent_claim_id", "parent_session_id", "root_work_key",
                           "replaces_hub_session_id", "stolen_from_claim_id", "stolen_from_session_id",
                           "stolen_reason", "prior_worktree_state", "handoff_to"}:
                return f"lineage has forbidden field {key}"
        for field, maximum in (("task_id", 64), ("parent_claim_id", 64), ("parent_session_id", 64),
                               ("root_work_key", 256), ("replaces_hub_session_id", 64),
                               ("stolen_from_claim_id", 64), ("stolen_from_session_id", 64),
                               ("handoff_to", 128)):
            if lineage.get(field) is not None and not _str_len(lineage.get(field), maximum):
                return f"lineage.{field} invalid"
        if lineage.get("stolen_reason") is not None and lineage["stolen_reason"] not in (
                "process_exited", "ttl_expired", "host_offline", "operator_override"):
            return "lineage.stolen_reason invalid"
        if lineage.get("prior_worktree_state") is not None and lineage["prior_worktree_state"] not in (
                "clean", "dirty_preserved", "not_found", "unknown"):
            return "lineage.prior_worktree_state invalid"
    if not _is_int(data.get("lease_ttl_sec")) or not 60 <= data["lease_ttl_sec"] <= 14400:
        return "lease_ttl_sec invalid"
    outcome = data.get("outcome")
    if outcome is not None:
        if not isinstance(outcome, dict):
            return "outcome must be an object"
        for key in outcome:
            if key not in {"status", "reason", "fix_commit_sha", "branch", "report_path", "dedup_cooldown_sec"}:
                return f"outcome has forbidden field {key}"
        if outcome.get("status") is not None and outcome["status"] not in (
                "completed", "abandoned", "handed_off", "stolen"):
            return "outcome.status invalid"
        if outcome.get("reason") is not None and not _str_len(outcome.get("reason"), 280):
            return "outcome.reason invalid"
        if outcome.get("fix_commit_sha") is not None and (
                not isinstance(outcome["fix_commit_sha"], str) or not _COMMIT_SHA_RE.match(outcome["fix_commit_sha"])):
            return "outcome.fix_commit_sha invalid"
        if outcome.get("branch") is not None and not _str_len(outcome.get("branch"), 128):
            return "outcome.branch invalid"
        if outcome.get("report_path") is not None and not _str_len(outcome.get("report_path"), 512):
            return "outcome.report_path invalid"
        if outcome.get("dedup_cooldown_sec") is not None and (
                not _is_int(outcome["dedup_cooldown_sec"]) or not 0 <= outcome["dedup_cooldown_sec"] <= 86400):
            return "outcome.dedup_cooldown_sec invalid"
    if data.get("summary") is not None and (
            not isinstance(data["summary"], str) or len(data["summary"]) > 280):
        return "summary invalid"
    return None


def native_actor(machine_id: str, tag: str, session_id: str = "") -> str:
    """Host-attested synthetic actor: ``native:<machine>:<tag>[<short>]``."""
    short = re.sub(r"[^0-9a-fA-F]", "", session_id)[:8]
    return f"native:{machine_id}:{tag}[{short}]" if short else f"native:{machine_id}:{tag}"


def synthesized_claim_id(session_id: str, workspace: str, work_key: str) -> str:
    """Stable host-synthesized claim id for declarations whose ``--task``
    string carries no ``[claim=...]`` marker. Deterministic per
    (session, workspace, work_key) so heartbeats upsert instead of duplicating.
    """
    digest = hashlib.sha256(f"{session_id}\x00{workspace}\x00{work_key}".encode("utf-8")).digest()
    alphabet = _HOST_CLAIM_ALPHABET
    num = int.from_bytes(digest[:12], "big")
    chars = ""
    base = len(alphabet)
    for _ in range(20):
        chars = alphabet[num % base] + chars
        num //= base
    return "clm_" + chars


def canonical_repo(path: str, git_bin: str = "git") -> str:
    """Resolve the canonical primary repository root (design 3.2).

    ``dirname $(git rev-parse --git-common-dir)`` when the path lives in a
    Git checkout whose common dir is named ``.git``; otherwise the real path.
    Never raises: falls back to the absolute path.
    """
    try:
        proc = subprocess.run(
            [git_bin, "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
        common = (proc.stdout or "").strip().splitlines()
        common_dir = common[0] if proc.returncode == 0 and common else ""
        if common_dir and os.path.basename(common_dir) == ".git":
            parent = os.path.dirname(common_dir)
            if parent:
                return os.path.realpath(parent)
    except Exception:
        pass
    try:
        return os.path.realpath(os.path.abspath(path))
    except Exception:
        return path


def _read_json_file(path: Path, limit: int) -> Optional[Any]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > limit or path.stat().st_size <= 0:
            return None
        return json.loads(path.read_bytes().decode("utf-8"))
    except Exception:
        return None


def _state_dir(explicit: Optional[str] = None) -> str:
    from .bridge import get_authoritative_state_dir
    try:
        return get_authoritative_state_dir(explicit)
    except Exception:
        home = os.environ.get("HOME", os.path.expanduser("~"))
        return os.path.join(home, ".local", "state", "aplexer")


def subprocess_runner(argv: list[str]) -> Any:
    """Minimal argv runner for one-shot CLI use (no shell, bounded output)."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("aplexer list failed")
    return json.loads(proc.stdout or "[]")


def _list_rows(runner: Any, aplexer_bin: str) -> dict[str, dict[str, Any]]:
    """Live session rows by session id (best-effort; {} when unavailable)."""
    try:
        raw = runner([aplexer_bin, "list", "--all", "--json"])
    except Exception:
        return {}
    rows = raw if isinstance(raw, list) else (raw.get("sessions") if isinstance(raw, dict) else None)
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
            out[row["id"]] = row
    return out


def scan_declarations(
    state_dir: Optional[str] = None,
    list_rows: Optional[dict[str, dict[str, Any]]] = None,
    machine_id: str = "",
) -> list[dict[str, Any]]:
    """Read every session's ``coordination.json`` declarations with session metadata.

    Returns declaration dicts: session_id, tag, engine, session_state
    (running/stale/exited/unknown), mode, scopes, task, work_key, claim_id,
    ttl_sec, summary, workspace, worktree, updated_at_ms.
    """
    root = Path(state_dir or _state_dir()) / "sessions"
    declarations: list[dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir())
    except Exception:
        return declarations
    for entry in entries[:1000]:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            coord = _read_json_file(entry / "coordination.json", 256 * 1024)
            if not isinstance(coord, dict):
                continue
            session_id = coord.get("session_id") or entry.name
            session_doc = _read_json_file(entry / "session.json", 128 * 1024)
            tag = ""
            engine = ""
            worktree = ""
            if isinstance(session_doc, dict):
                tag = session_doc.get("tag") or ""
                engine = session_doc.get("engine") or ""
                worktree = session_doc.get("workspace") or session_doc.get("cwd") or ""
            live = (list_rows or {}).get(session_id, {}) if isinstance(session_id, str) else {}
            phase = live.get("phase") or live.get("state") or ""
            if phase in ("Exited", "Failed", "exited", "failed"):
                session_state = "exited"
            elif phase and phase not in ("Running", "running"):
                session_state = "stale"
            elif live.get("worker_alive") is False:
                session_state = "stale"
            elif live:
                session_state = "running"
            else:
                session_state = "unknown"
            items = coord.get("declarations")
            if not isinstance(items, list):
                continue
            for decl in items[:64]:
                if not isinstance(decl, dict):
                    continue
                task = decl.get("task") if isinstance(decl.get("task"), str) else ""
                parsed = parse_work_task_string(task)
                mode = decl.get("mode") if decl.get("mode") in ("edit", "read", "review") else "read"
                scopes = [s for s in (decl.get("scopes") or []) if isinstance(s, str)][:64]
                updated = decl.get("updated_at_ms")
                declarations.append({
                    "session_id": session_id if isinstance(session_id, str) else entry.name,
                    "tag": tag if isinstance(tag, str) else "",
                    "engine": engine if isinstance(engine, str) else "",
                    "machine_id": machine_id,
                    "session_state": session_state,
                    "mode": mode,
                    "scopes": scopes,
                    "task": task,
                    "work_key": parsed["work_key"],
                    "claim_id": parsed["claim_id"],
                    "ttl_sec": parsed["ttl_sec"] or DEFAULT_LEASE_SEC,
                    "summary": parsed["summary"],
                    "workspace": decl.get("workspace") if isinstance(decl.get("workspace"), str) else "",
                    "worktree": worktree,
                    "updated_at_ms": updated if isinstance(updated, int) and updated > 0 else None,
                })
        except Exception:
            continue
    return declarations


def _mailbox_workspace(ws_dir: Path) -> Optional[str]:
    meta = _read_json_file(ws_dir / "workspace.json", 8192)
    if not isinstance(meta, dict):
        return None
    workspace = meta.get("workspace")
    return workspace if isinstance(workspace, str) and workspace else None


def resolve_mailbox_canonical(
    mailbox_workspace: str,
    canonical_workspaces: dict[str, str],
    git_cache: dict[str, str],
) -> Optional[str]:
    """Map a mailbox workspace to its canonical repo root.

    Direct equality first; otherwise compare ``git_common_dir`` parents so
    temporary-worktree mailboxes resolve to the permanent primary checkout
    (design 3.2). A cheap prefix prefilter avoids shelling out to git for
    unrelated mailboxes; the git common-dir check stays authoritative.
    Returns the canonical path or None when unmapped.
    """
    if mailbox_workspace in canonical_workspaces:
        return mailbox_workspace
    related = any(
        mailbox_workspace.startswith(canonical + "-") or mailbox_workspace.startswith(canonical + "/")
        for canonical in canonical_workspaces
    )
    if not related:
        return None
    if mailbox_workspace not in git_cache:
        git_cache[mailbox_workspace] = canonical_repo(mailbox_workspace)
    canonical = git_cache[mailbox_workspace]
    return canonical if canonical in canonical_workspaces else None


def scan_work_messages(
    state_dir: Optional[str] = None,
    canonical_workspaces: Optional[dict[str, str]] = None,
    cursors: Optional[dict[str, str]] = None,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, int]]:
    """Read new ``work.*`` envelopes from canonical workspace mailboxes.

    Returns (messages, new_cursors, stats). Each message: native_id, kind,
    body, data, from{...}, mailbox workspace, created_at_ms. The cursor per
    mailbox is the greatest envelope id observed (UUIDv7 sorts by time), so
    rescans after a restart resend at most a bounded duplicate window which
    the server deduplicates via ``aplexer-msg:<id>`` idempotency keys.
    """
    root = Path(state_dir or _state_dir()) / "messages"
    canonicals = canonical_workspaces or {}
    cursor_map = dict(cursors or {})
    git_cache: dict[str, str] = {}
    messages: list[dict[str, Any]] = []
    stats = {"mailboxes": 0, "envelopes": 0, "skipped_kind": 0, "skipped_invalid": 0, "mailbox_cap_hit": 0}
    try:
        entries = sorted(root.iterdir())
    except Exception:
        return messages, cursor_map, stats
    matched = 0
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            mailbox_workspace = _mailbox_workspace(entry)
            if not mailbox_workspace:
                continue
            canonical = resolve_mailbox_canonical(mailbox_workspace, canonicals, git_cache)
            if canonical is None:
                continue
            if matched >= MAX_MAILBOXES_PER_SCAN:
                stats["mailbox_cap_hit"] += 1
                continue
            matched += 1
            stats["mailboxes"] += 1
            msgs_dir = entry / "msgs"
            try:
                names = sorted(p.name for p in msgs_dir.iterdir() if p.is_file() and p.suffix == ".json")
            except Exception:
                continue
            cursor = cursor_map.get(entry.name, "")
            newest = cursor
            for name in names[-MAX_MSGS_PER_MAILBOX:]:
                envelope_id = name[:-len(".json")]
                if envelope_id <= cursor:
                    continue
                if envelope_id > newest:
                    newest = envelope_id
                doc = _read_json_file(msgs_dir / name, 512 * 1024)
                if not isinstance(doc, dict):
                    stats["skipped_invalid"] += 1
                    continue
                stats["envelopes"] += 1
                kind = doc.get("kind")
                if kind not in WORK_MESSAGE_KINDS:
                    stats["skipped_kind"] += 1
                    continue
                messages.append({
                    "native_id": doc.get("id") or envelope_id,
                    "kind": kind,
                    "body": doc.get("body") if isinstance(doc.get("body"), str) else "",
                    "data": doc.get("data"),
                    "from": doc.get("from") if isinstance(doc.get("from"), dict) else {},
                    "mailbox_workspace": mailbox_workspace,
                    "canonical_workspace": canonical,
                    "created_at_ms": doc.get("created_at_ms")
                    if isinstance(doc.get("created_at_ms"), int)
                    else (doc.get("created_at") * 1000 if isinstance(doc.get("created_at"), int) else None),
                })
            cursor_map[entry.name] = newest
        except Exception:
            continue
    return messages, cursor_map, stats


def project_for_workspace(canonical_workspace: str) -> str:
    """Hub project slug for a canonical repo path (basename heuristic)."""
    return os.path.basename(canonical_workspace.rstrip("/")) or canonical_workspace


def build_claim_items(
    declarations: list[dict[str, Any]],
    canonical_workspaces: dict[str, str],
    machine_id: str,
    now_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build native-sync claim items from live declarations.

    Only declarations whose workspace resolves to a canonical repo (directly
    or via git_common_dir) and whose work_key parses are synced; read/review
    declarations sync as non-exclusive observations. Returns (items, stats).
    """
    git_cache: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    stats = {"declarations": len(declarations), "unmapped": 0, "no_work_key": 0, "invalid": 0}
    for decl in declarations:
        workspace = decl.get("workspace") or ""
        canonical = resolve_mailbox_canonical(workspace, canonical_workspaces, git_cache)
        if canonical is None:
            stats["unmapped"] += 1
            continue
        work_key = decl.get("work_key")
        if not isinstance(work_key, str) or not _WORK_KEY_RE.match(work_key):
            # Derive a path-scoped key so file ownership is still visible.
            # Whole-repo scopes ('*', '**') map to the 'all' leaf.
            first_scope = next((s for s in (decl.get("scopes") or []) if isinstance(s, str) and s), "*")
            safe_scope = re.sub(r"[^A-Za-z0-9._/-]", "_", first_scope.replace("*", "")).strip("/")[:128] or "all"
            work_key = f"path:{project_for_workspace(canonical)}:{safe_scope}"
        project = project_for_workspace(canonical)
        if not _PROJECT_RE.match(project):
            stats["invalid"] += 1
            continue
        claim_id = decl.get("claim_id") or synthesized_claim_id(
            str(decl.get("session_id") or ""), workspace, work_key)
        if not _CLAIM_ID_RE.match(claim_id):
            stats["invalid"] += 1
            continue
        lease = decl.get("ttl_sec") or DEFAULT_LEASE_SEC
        try:
            lease = int(lease)
        except (TypeError, ValueError):
            lease = DEFAULT_LEASE_SEC
        lease = max(60, min(14400, lease))
        updated = decl.get("updated_at_ms") or now_ms
        state = decl.get("session_state") or "unknown"
        if state == "exited":
            remaining = 0
        else:
            remaining = max(0, (updated + (lease + LEASE_GRACE_SEC) * 1000) - now_ms)
        scopes = [s for s in (decl.get("scopes") or [])
                  if isinstance(s, str) and s and len(s) <= 512 and ".." not in s
                  and "\\" not in s and not s.startswith("/")]
        data: dict[str, Any] = {
            "schema": WORK_CLAIM_SCHEMA,
            "event": "claim",
            "claim_id": claim_id,
            "seq": 1,
            "work_key": work_key,
            "project": project,
            "workspace": canonical,
            "mode": decl.get("mode") or "read",
            "scopes": scopes,
            "holder": {
                "machine_id": machine_id,
                "session_id": str(decl.get("session_id") or "")[:64],
                "tag": str(decl.get("tag") or "unknown")[:128],
                **({"engine": str(decl.get("engine"))[:64]} if decl.get("engine") else {}),
            },
            "lease_ttl_sec": lease,
            "summary": str(decl.get("summary") or decl.get("task") or "")[:280],
        }
        if decl.get("worktree"):
            data["worktree"] = str(decl["worktree"])[:1024]
        reason = validate_work_claim_data(data)
        if reason:
            stats["invalid"] += 1
            continue
        items.append({
            "data": redact_claim_data(data),
            "remaining_ttl_ms": remaining,
            "session_state": "running" if state == "running" else ("exited" if state == "exited" else "stale"),
        })
        if len(items) >= MAX_ITEMS_PER_SYNC:
            break
    return items, stats


def build_message_items(
    envelopes: list[dict[str, Any]],
    machine_id: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build native-sync message items from work.* envelopes.

    Envelopes whose structured data validates contribute project/work_key
    routing; every work.* envelope mirrors its redacted body either way so
    operators see the lifecycle event. Invalid rows are skipped with a count.
    """
    items: list[dict[str, Any]] = []
    stats = {"envelopes": len(envelopes), "invalid": 0}
    for env in envelopes:
        data = env.get("data")
        project: Optional[str] = None
        if isinstance(data, dict) and validate_work_claim_data(data) is None:
            candidate = data.get("project")
            if isinstance(candidate, str) and _PROJECT_RE.match(candidate):
                project = candidate
        if project is None:
            project = project_for_workspace(env.get("canonical_workspace") or "")
            if not _PROJECT_RE.match(project):
                stats["invalid"] += 1
                continue
        sender = env.get("from") or {}
        tag = sender.get("tag") if isinstance(sender.get("tag"), str) else ""
        session_id = sender.get("session_id") if isinstance(sender.get("session_id"), str) else ""
        if not tag or not _ACTOR_SEG_RE.match(tag):
            stats["invalid"] += 1
            continue
        body = env.get("body")
        if not isinstance(body, str) or not body:
            # Fall back to a compact lifecycle line so terminal events
            # without a body still surface (kind + claim id when known).
            claim_ref = ""
            if isinstance(data, dict) and isinstance(data.get("claim_id"), str):
                claim_ref = f" {data['claim_id']}"
            body = f"{env.get('kind')}{claim_ref}".strip()
        if len(body) > 4000:
            stats["invalid"] += 1
            continue
        native_id = env.get("native_id")
        if not isinstance(native_id, str) or not native_id or len(native_id) > 64:
            stats["invalid"] += 1
            continue
        items.append({
            "native_id": native_id,
            "kind": env.get("kind"),
            "body": redact_for_bus(body),
            "project": project,
            "from": {
                "machine_id": machine_id,
                "session_id": session_id[:64],
                "tag": tag[:128],
                **({"engine": str(sender["engine"])[:64]} if isinstance(sender.get("engine"), str) and sender.get("engine") else {}),
            },
            **({"created_at_ms": env["created_at_ms"]} if isinstance(env.get("created_at_ms"), int) else {}),
        })
        if len(items) >= MAX_ITEMS_PER_SYNC:
            break
    return items, stats


def _cursors_path(journal_dir: Optional[str]) -> Optional[Path]:
    if not journal_dir or not isinstance(journal_dir, str):
        return None
    try:
        target = Path(journal_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target / "workclaim-cursors.json"
    except Exception:
        return None


def load_cursors(journal_dir: Optional[str]) -> dict[str, str]:
    path = _cursors_path(journal_dir)
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        pass
    return {}


def save_cursors(journal_dir: Optional[str], cursors: dict[str, str]) -> None:
    path = _cursors_path(journal_dir)
    if path is None:
        return
    try:
        from . import private_fs
        private_fs.atomic_write(str(path), json.dumps(cursors, sort_keys=True).encode("utf-8"), 65536)
    except Exception:
        try:
            path.write_bytes(json.dumps(cursors, sort_keys=True).encode("utf-8"))
        except Exception:
            pass


def canonical_workspaces_from_settings(settings: Any) -> dict[str, str]:
    """Operator-configured workspace roots, canonicalized (path -> canonical)."""
    out: dict[str, str] = {}
    try:
        workspaces = (settings or {}).get("workspaces", {})
    except AttributeError:
        return out
    if not isinstance(workspaces, dict):
        return out
    for _key, value in workspaces.items():
        if not isinstance(value, str) or not value:
            continue
        try:
            if not os.path.isabs(value):
                continue
            canonical = os.path.realpath(os.path.abspath(value))
            out[canonical] = canonical
        except Exception:
            continue
    return out


def sync_once(
    request: Any,
    settings: Any = None,
    runner: Any = None,
    aplexer_bin: Optional[str] = None,
    state_dir: Optional[str] = None,
    machine_id: Optional[str] = None,
    journal_dir: Optional[str] = None,
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """One best-effort native work-claim sync cycle. Never raises.

    Reads local declarations + work.* envelopes, redacts, and POSTs to
    ``/api/bus/native-sync``. Returns a bounded summary dict for the poll
    cycle log.
    """
    started = time.monotonic()
    summary: dict[str, Any] = {"ok": False}
    try:
        settings = settings or {}
        binary = aplexer_bin or settings.get("aplexer_bin", "aplexer")
        resolved_state = state_dir or settings.get("aplexer_state_dir") or _state_dir()
        host_machine = machine_id or settings.get("machine_id", "") or ""
        if not isinstance(host_machine, str) or not host_machine or not _MACHINE_SEG_RE.match(host_machine):
            try:
                import socket
                host_machine = socket.gethostname().split(".")[0][:64] or "local"
            except Exception:
                host_machine = "local"
        moment = now_ms if isinstance(now_ms, int) and now_ms > 0 else int(time.time() * 1000)
        canonicals = canonical_workspaces_from_settings(settings)
        rows = _list_rows(runner or subprocess_runner, binary)
        declarations = scan_declarations(resolved_state, rows, host_machine)
        cursors = load_cursors(journal_dir if journal_dir is not None else settings.get("journal_dir"))
        envelopes, new_cursors, scan_stats = scan_work_messages(resolved_state, canonicals, cursors)
        claim_items, claim_stats = build_claim_items(declarations, canonicals, host_machine, moment)
        message_items, message_stats = build_message_items(envelopes, host_machine)
        posted: dict[str, Any] = {"synced_claims": 0, "synced_messages": 0}
        if claim_items or message_items:
            try:
                res = request("POST", "/api/bus/native-sync", {"claims": claim_items, "messages": message_items})
                if isinstance(res, dict):
                    posted = res
            except Exception as exc:
                summary.update({
                    "ok": False,
                    "error": f"native_sync_post_failed:{type(exc).__name__}"[:120],
                    "claims": len(claim_items),
                    "messages": len(message_items),
                })
                return summary
            save_cursors(journal_dir if journal_dir is not None else settings.get("journal_dir"), new_cursors)
        summary.update({
            "ok": True,
            "claims": len(claim_items),
            "messages": len(message_items),
            "posted": {k: posted.get(k) for k in ("synced_claims", "released_claims", "synced_messages", "deduped_messages")},
            "declaration_stats": claim_stats,
            "envelope_stats": {**scan_stats, "invalid": message_stats.get("invalid", 0)},
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
    except Exception as exc:
        summary.update({"ok": False, "error": (type(exc).__name__ or "Error")[:64]})
    return summary
