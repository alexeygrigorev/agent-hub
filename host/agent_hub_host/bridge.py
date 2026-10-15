"""Aplexer mailbox bridge (docs/SPEC.md §7.5).

Bridges hub messages to native aplexer mailboxes and native replies/ACKs
back to the hub, strictly respecting authoritative aplexer state layout,
metadata verification, and identity boundaries.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import subprocess
import time
import uuid
from typing import Any

from . import bus
from .native_bridge import (
    BridgeCapabilityGap,
    NativeServiceBinding,
    qualify_native_service_binding,
    read_authoritative_cursor_safe,
    validate_native_reply_provenance,
    validate_native_send_envelope,
)

MAX_RECEIPTS_ENTRIES = 1000
MAX_PENDING_INBOX = 1000

# Message kinds the hub accepts on /api/bus/send and /api/bus/reply (SPEC §6). A native reply
# whose kind is outside this set is bridged back as a plain "reply" instead of being retried
# against a 400 forever. Native "sync_reply" responses stay plain replies: only an explicit
# hub request may opt into guarded synchronous delivery, never a normalized native response.
HUB_MESSAGE_KINDS = frozenset(["note", "handoff", "reply", "status", "claim", "release"])

# Prompt glyphs an idle, empty agent composer shows; box-drawing borders around it are ignored.
PROMPT_GLYPHS = frozenset(["❯", ">", "$", "#"])
COMPOSER_BORDER_CHARS = "│┃║|╭╮╰╯─━═ \t"

_ADDRESS_SEGMENT_FORBIDDEN = set(":") | {chr(c) for c in range(0x20)} | {"\x7f"}


def _valid_address_segment(segment: str) -> bool:
    return bool(segment) and not any(ch in _ADDRESS_SEGMENT_FORBIDDEN or ch.isspace() for ch in segment)


def native_tag_for_address(address: Any) -> str | None:
    """Map a hub agent address to the native aplexer tag, or None if it has no native mailbox.

    Mirrors the hub address grammar (src/bus.ts parseAddress): `tag` -> tag, `project:tag` -> tag.
    `role:*` and `machine:*` are not native mailbox tags, and malformed addresses (empty
    segments, more than one ':', whitespace) map to nothing rather than to a guessed tag.
    """
    if not isinstance(address, str) or not address or len(address) > 128:
        return None
    parts = address.split(":")
    if not all(_valid_address_segment(p) for p in parts):
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) != 2 or parts[0] in ("role", "machine"):
        return None
    return parts[1]


def normalize_native_kind(kind: Any) -> str:
    """Kind to use when bridging a native message back to the hub."""
    return kind if isinstance(kind, str) and kind in HUB_MESSAGE_KINDS else "reply"


def composer_is_empty(last_line: str) -> bool:
    """True only for an idle, empty prompt line (inside optional box borders).

    Accepted: a bare prompt glyph, or one label token followed by the agent prompt glyph
    ("user@host:~$ ❯"); a composer shows draft text after its glyph, never before it.
    Refused: any text after a glyph, '>', '$' or '#' with any text (a draft such as
    "echo hi >" is indistinguishable from a label there), and multi-word text before '❯'.
    """
    line = last_line.strip(COMPOSER_BORDER_CHARS)
    if line in PROMPT_GLYPHS:
        return True
    tokens = line.split()
    return len(tokens) == 2 and tokens[1] == "❯" and tokens[0] not in PROMPT_GLYPHS


def get_authoritative_runtime_dir(explicit_runtime_dir: str | None = None) -> str:
    """Resolve authoritative native runtime directory per aplexer paths.rs.

    Precedence:
    explicit runtime_dir > APLEXER_RUNTIME_DIR > XDG_RUNTIME_DIR/aplexer > /tmp/aplexer-<uid>.
    Never checks workspace-local directories for runtime root.
    """
    if explicit_runtime_dir:
        return os.path.abspath(explicit_runtime_dir)
    if os.environ.get("APLEXER_RUNTIME_DIR"):
        return os.path.abspath(os.environ["APLEXER_RUNTIME_DIR"])
    if os.environ.get("XDG_RUNTIME_DIR"):
        return os.path.abspath(os.path.join(os.environ["XDG_RUNTIME_DIR"], "aplexer"))
    uid = os.geteuid() if hasattr(os, "geteuid") else 1000
    return os.path.abspath(f"/tmp/aplexer-{uid}")


def get_authoritative_state_dir(explicit_state_dir: str | None = None) -> str:
    """Resolve authoritative native state directory per aplexer paths.rs.

    Precedence:
    explicit state_dir > APLEXER_STATE_DIR > XDG_STATE_HOME/aplexer > ~/.local/state/aplexer.
    Never checks workspace-local directories for state root.
    """
    if explicit_state_dir:
        return os.path.abspath(explicit_state_dir)
    if os.environ.get("APLEXER_STATE_DIR"):
        return os.path.abspath(os.environ["APLEXER_STATE_DIR"])
    if os.environ.get("XDG_STATE_HOME"):
        return os.path.abspath(os.path.join(os.environ["XDG_STATE_HOME"], "aplexer"))
    home = os.environ.get("HOME", os.path.expanduser("~"))
    return os.path.abspath(os.path.join(home, ".local", "state", "aplexer"))


def canonicalize_workspace(workspace_path: str) -> str:
    """Canonicalize workspace path matching aplexer's canonical_workspace."""
    abs_path = os.path.abspath(workspace_path)
    try:
        return os.path.realpath(abs_path)
    except Exception:
        return abs_path


def get_workspace_key(canonical_workspace_path: str) -> str:
    """Compute mailbox directory key: first 128 bits of SHA-256 (32 hex characters)."""
    return hashlib.sha256(canonical_workspace_path.encode("utf-8")).hexdigest()[:32]


class MailboxBridge:
    def __init__(
        self,
        workspace_path: str,
        aplexer_bin: str = "aplexer",
        state_dir: str | None = None,
        runtime_dir: str | None = None,
        service_tag: str | None = None,
        service_session_id: str | None = None,
    ):
        self.workspace = os.path.abspath(workspace_path)
        self.canonical_workspace = canonicalize_workspace(self.workspace)
        self.aplexer_bin = aplexer_bin
        self.state_dir = state_dir
        self.runtime_dir = runtime_dir
        self.service_tag = service_tag
        self.service_session_id = service_session_id
        self.service_binding = qualify_native_service_binding(
            self.canonical_workspace,
            state_dir=get_authoritative_state_dir(self.state_dir),
            runtime_dir=get_authoritative_runtime_dir(self.runtime_dir),
            service_tag=service_tag,
            service_session_id=service_session_id,
        )
        self.receipts_file = os.path.join(self.workspace, ".local", "hub-bridge-receipts.json")
        self.lock_file = os.path.join(self.workspace, ".local", "hub-bridge-receipts.lock")
        self.receipts = self._load_receipts()

    @contextmanager
    def _receipts_lock(self):
        lock_dir = os.path.dirname(self.lock_file) or "."
        os.makedirs(lock_dir, exist_ok=True)
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load_receipts(self) -> dict[str, Any]:
        if os.path.exists(self.receipts_file):
            try:
                with open(self.receipts_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise BridgeCapabilityGap(f"Receipts file {self.receipts_file} is corrupt or unreadable: {e}") from e
            if not isinstance(data, dict):
                raise BridgeCapabilityGap(
                    f"Receipts file {self.receipts_file} is malformed: root must be a dict, got {type(data).__name__}"
                )
            data.setdefault("hub_to_native", {})
            data.setdefault("native_to_hub", {})
            data.setdefault("hub_messages", {})
            data.setdefault("acked_hub_messages", [])
            data.setdefault("ambiguous_sends", {})
            data.setdefault("delivers", {})
            if not isinstance(data.setdefault("inbox_cursors", {}), dict):
                raise BridgeCapabilityGap(f"Receipts file {self.receipts_file} has malformed inbox_cursors")
            for key in ("inbox_pending", "inbox_scans"):
                if not isinstance(data.setdefault(key, {}), dict):
                    raise BridgeCapabilityGap(f"Receipts file {self.receipts_file} has malformed {key}")
            for pending in data["inbox_pending"].values():
                if (not isinstance(pending, dict) or len(pending) > MAX_PENDING_INBOX
                        or any(not isinstance(row, dict) or row.get("id") != mid for mid, row in pending.items())):
                    raise BridgeCapabilityGap(f"Receipts file {self.receipts_file} has malformed inbox_pending rows")
            return data
        return {
            "hub_to_native": {},
            "native_to_hub": {},
            "hub_messages": {},
            "acked_hub_messages": [],
            "ambiguous_sends": {},
            "delivers": {},
            "inbox_cursors": {},
        }

    def _prune_receipts(self) -> None:
        """Bound existing receipt storage retention without losing active uncertain/unacked state."""
        # 1. Bound acked_hub_messages
        acked = self.receipts.get("acked_hub_messages", [])
        if len(acked) > MAX_RECEIPTS_ENTRIES:
            self.receipts["acked_hub_messages"] = acked[-MAX_RECEIPTS_ENTRIES:]

        # 2. Bound completed hub_to_native, hub_messages, and delivers by pruning oldest acked messages
        hub_to_native = self.receipts.get("hub_to_native", {})
        if len(hub_to_native) > MAX_RECEIPTS_ENTRIES:
            acked_set = set(self.receipts.get("acked_hub_messages", []))
            excess = len(hub_to_native) - MAX_RECEIPTS_ENTRIES
            pruned = 0
            for hid in list(hub_to_native.keys()):
                if hid in acked_set:
                    nid = hub_to_native.pop(hid, None)
                    if nid:
                        self.receipts.get("hub_messages", {}).pop(nid, None)
                        deliv = self.receipts.get("delivers", {}).get(nid)
                        if deliv and deliv.get("status") == "delivered":
                            self.receipts.get("delivers", {}).pop(nid, None)
                    pruned += 1
                    if pruned >= excess:
                        break

        # 3. Bound native_to_hub
        native_to_hub = self.receipts.get("native_to_hub", {})
        if len(native_to_hub) > MAX_RECEIPTS_ENTRIES:
            excess = len(native_to_hub) - MAX_RECEIPTS_ENTRIES
            for k in list(native_to_hub.keys())[:excess]:
                del native_to_hub[k]

    def _sync_receipts_with_lock(self) -> None:
        if not hasattr(self, "receipts") or self.receipts is None:
            self.receipts = self._load_receipts()
            return
        if not self.receipts:
            # Explicitly cleared in memory (e.g. test isolation)
            return
        if os.path.exists(self.receipts_file):
            disk = self._load_receipts()
            for k in ["hub_to_native", "native_to_hub", "hub_messages", "ambiguous_sends", "delivers"]:
                if k in self.receipts:
                    disk[k].update(self.receipts[k])
                self.receipts[k] = disk[k]
            for a in self.receipts.get("acked_hub_messages", []):
                if a not in disk["acked_hub_messages"]:
                    disk["acked_hub_messages"].append(a)
            self.receipts["acked_hub_messages"] = disk["acked_hub_messages"]
            # Inbox cursors only ever move forward, whichever process wrote last
            for address, cursor in (self.receipts.get("inbox_cursors") or {}).items():
                if isinstance(cursor, str) and cursor > disk["inbox_cursors"].get(address, ""):
                    disk["inbox_cursors"][address] = cursor
            self.receipts["inbox_cursors"] = disk["inbox_cursors"]
            for key in ("inbox_pending", "inbox_scans"):
                self.receipts[key] = disk[key]

    def inbox_cursor(self, address: str) -> str | None:
        """Durable hub inbox cursor for `address`: every row before it is settled or durably pending."""
        cursor = (self.receipts.get("inbox_cursors") or {}).get(address)
        return cursor if isinstance(cursor, str) and cursor else None

    def advance_inbox_cursor(self, address: str, cursor: str) -> None:
        """Persist a later inbox cursor for `address`; an earlier or equal one is ignored."""
        with self._receipts_lock():
            self._sync_receipts_with_lock()
            cursors = self.receipts.setdefault("inbox_cursors", {})
            if cursor > cursors.get(address, ""):
                cursors[address] = cursor
                self._save_receipts()

    def pending_inbox(self, address: str) -> dict[str, Any]:
        with self._receipts_lock():
            self._sync_receipts_with_lock()
            return dict(self.receipts.get("inbox_pending", {}).get(address, {}))

    def remember_inbox(self, address: str, msg: dict[str, Any]) -> None:
        """Durably retain a row before attempting an effect or moving the scan past it."""
        with self._receipts_lock():
            self._sync_receipts_with_lock()
            pending = self.receipts.setdefault("inbox_pending", {}).setdefault(address, {})
            if msg["id"] not in pending and len(pending) >= MAX_PENDING_INBOX:
                raise BridgeCapabilityGap("pending inbox capacity exhausted; scan held until delivery or expiry")
            pending[msg["id"]] = dict(msg)
            self._save_receipts()

    def forget_inbox(self, address: str, hub_id: str) -> None:
        """Remove only a legitimately settled or expired pending row."""
        with self._receipts_lock():
            self._sync_receipts_with_lock()
            self.receipts.setdefault("inbox_pending", {}).setdefault(address, {}).pop(hub_id, None)
            self._save_receipts()

    def inbox_scan(self, address: str) -> str | None:
        cursor = self.receipts.get("inbox_scans", {}).get(address)
        return cursor if isinstance(cursor, str) and cursor else self.inbox_cursor(address)

    def save_inbox_scan(self, address: str, cursor: str | None) -> None:
        with self._receipts_lock():
            self._sync_receipts_with_lock()
            self.receipts.setdefault("inbox_scans", {})[address] = cursor
            self._save_receipts()

    def _save_receipts(self) -> None:
        self._prune_receipts()
        dir_path = os.path.dirname(self.receipts_file) or "."
        os.makedirs(dir_path, exist_ok=True)
        tmp_file = self.receipts_file + ".tmp"
        fd = os.open(tmp_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(self.receipts, f, indent=2)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise
        os.replace(tmp_file, self.receipts_file)

    def _verify_mailbox_dir(self, state_root: str) -> str | None:
        """Verify and return the authoritative mailbox directory for this canonical workspace."""
        ws_key = get_workspace_key(self.canonical_workspace)
        ws_dir = os.path.join(state_root, "messages", ws_key)
        if not os.path.isdir(ws_dir):
            return None

        meta_file = os.path.join(ws_dir, "workspace.json")
        if not os.path.isfile(meta_file):
            return None

        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                return None
            meta_ws = meta.get("workspace")
            if not meta_ws or not isinstance(meta_ws, str):
                return None
            canonical_meta_ws = canonicalize_workspace(meta_ws)
            if canonical_meta_ws != self.canonical_workspace:
                return None
            if get_workspace_key(canonical_meta_ws) != ws_key:
                return None
            return ws_dir
        except Exception:
            return None

    def _resolve_recipient_session_id(self, arg1: str, arg2: Any = None, arg3: Any = None, state_root: str | None = None) -> str | None:
        """Bind to actual registered recipient native session UUID using read-only authoritative metadata."""
        if isinstance(arg2, str):
            effective_state_root = arg1
            to_actor = arg2
            hub_msg_info = arg3 if isinstance(arg3, dict) else {}
        else:
            effective_state_root = state_root or get_authoritative_state_dir(self.state_dir)
            to_actor = arg1
            hub_msg_info = arg2 if isinstance(arg2, dict) else {}

        runtime_root = get_authoritative_runtime_dir(self.runtime_dir)
        sessions_dirs = []
        if runtime_root and os.path.isdir(os.path.join(runtime_root, "sessions")):
            s_dir = os.path.join(runtime_root, "sessions")
            if not os.path.islink(s_dir):
                sessions_dirs.append(s_dir)
        if effective_state_root and os.path.isdir(os.path.join(effective_state_root, "sessions")):
            s_dir = os.path.join(effective_state_root, "sessions")
            if not os.path.islink(s_dir):
                sessions_dirs.append(s_dir)

        # 1. Check if previously resolved and still recorded in authoritative sessions
        recorded = hub_msg_info.get("recipient_session_id")
        if recorded and isinstance(recorded, str):
            for sdir in sessions_dirs:
                entry_dir = os.path.join(sdir, recorded)
                if os.path.islink(entry_dir) or not os.path.isdir(entry_dir):
                    continue
                rec_file = os.path.join(entry_dir, "session.json")
                if os.path.isfile(rec_file) and not os.path.islink(rec_file):
                    try:
                        with open(rec_file, "r", encoding="utf-8") as f:
                            rec = json.load(f)
                        if isinstance(rec, dict) and rec.get("workspace") and canonicalize_workspace(rec["workspace"]) == self.canonical_workspace:
                            return recorded
                    except Exception:
                        pass

        # 2. Resolve from authoritative session records
        target_tag = hub_msg_info.get("native_tag") or native_tag_for_address(to_actor)
        if not target_tag:
            return None
        matching_sessions = []
        seen_ids = set()

        for sdir in sessions_dirs:
            try:
                for entry in os.listdir(sdir):
                    entry_dir = os.path.join(sdir, entry)
                    if not os.path.isdir(entry_dir) or os.path.islink(entry_dir):
                        continue
                    rec_file = os.path.join(entry_dir, "session.json")
                    if not os.path.isfile(rec_file) or os.path.islink(rec_file):
                        continue
                    try:
                        with open(rec_file, "r", encoding="utf-8") as f:
                            rec = json.load(f)
                        if not isinstance(rec, dict):
                            continue
                        rec_ws = rec.get("workspace")
                        if not rec_ws or canonicalize_workspace(rec_ws) != self.canonical_workspace:
                            continue

                        rec_id = str(rec.get("id", ""))
                        if not rec_id or rec_id in seen_ids:
                            continue
                        seen_ids.add(rec_id)
                        rec_tag = str(rec.get("tag", ""))

                        # Matching priority:
                        # 0: exact session ID
                        # 1: prefix session ID (>= 8 chars)
                        # 2: tag match
                        if target_tag.lower() == rec_id.lower():
                            matching_sessions.append((rec, 0))
                        elif len(target_tag) >= 8 and rec_id.lower().startswith(target_tag.lower()):
                            matching_sessions.append((rec, 1))
                        elif rec_tag == target_tag:
                            matching_sessions.append((rec, 2))
                    except Exception:
                        continue
            except Exception:
                continue

        if not matching_sessions:
            return None

        # Sort by priority ascending, then created_at_ms descending (aplexer registry.rs convention)
        matching_sessions.sort(key=lambda item: (item[1], -item[0].get("created_at_ms", 0)))
        best_record = matching_sessions[0][0]
        resolved_id = str(best_record["id"])
        hub_msg_info["recipient_session_id"] = resolved_id
        self._save_receipts()
        return resolved_id

    def _is_native_id_positively_acked(self, native_id: str, to_actor: str, hub_msg_info: dict[str, Any]) -> bool:
        """Determine positive ACK strictly from authoritative recipient cursor."""
        state_root = get_authoritative_state_dir(self.state_dir)
        if not state_root or not os.path.isdir(state_root):
            return False

        ws_dir = self._verify_mailbox_dir(state_root)
        if not ws_dir:
            return False

        recipient_session_id = self._resolve_recipient_session_id(to_actor, hub_msg_info, state_root=state_root)
        if not recipient_session_id:
            return False

        cursor_path = os.path.join(ws_dir, "cursors", f"{recipient_session_id}.json")
        cdata = read_authoritative_cursor_safe(cursor_path)
        if not cdata:
            return False

        # Check exact exceptions
        exceptions = cdata.get("exceptions", [])
        if isinstance(exceptions, list) and native_id in exceptions:
            return True

        # Check legacy acked_through for this recipient only
        through = cdata.get("acked_through")
        if through and isinstance(through, str):
            try:
                nid_u = uuid.UUID(str(native_id))
                through_u = uuid.UUID(str(through))
                if nid_u <= through_u:
                    return True
            except (ValueError, TypeError):
                pass

        return False

    def _get_subprocess_env(self) -> dict[str, str]:
        sub_env = os.environ.copy()
        sub_env["APLEXER_WORKSPACE"] = self.canonical_workspace
        if self.state_dir:
            sub_env["APLEXER_STATE_DIR"] = get_authoritative_state_dir(self.state_dir)
        if self.runtime_dir:
            sub_env["APLEXER_RUNTIME_DIR"] = get_authoritative_runtime_dir(self.runtime_dir)
        return sub_env

    def _check_idle_and_empty_composer(self, tag: str) -> None:
        """Check status is idle and screen capture composer is empty with no drafts/menus."""
        sub_env = self._get_subprocess_env()
        st = subprocess.run(
            [self.aplexer_bin, "status", tag, "--workspace", self.workspace, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=sub_env,
        )
        if st.returncode != 0:
            raise BridgeCapabilityGap(f"Status check failed: {st.stderr[:200]}")
        try:
            s_data = json.loads(st.stdout)
        except Exception:
            raise BridgeCapabilityGap("Invalid status json")

        state = s_data.get("state", "unknown")
        if state != "idle":
            raise BridgeCapabilityGap(f"Session not idle: {state}")

        cap = subprocess.run(
            [self.aplexer_bin, "capture", tag, "--workspace", self.workspace, "--screen", "--plain"],
            capture_output=True,
            text=True,
            timeout=30,
            env=sub_env,
        )
        if cap.returncode != 0:
            raise BridgeCapabilityGap(f"Capture failed: {cap.stderr[:200]}")

        raw_stdout = cap.stdout
        lines = [l.rstrip() for l in raw_stdout.split("\n") if l.strip()]
        if not lines:
            raise BridgeCapabilityGap("Screen capture is empty")

        last_line = lines[-1].strip()

        # Menus / drafts refusal
        for line in lines[-5:]:
            l_str = line.strip().lower()
            if l_str.startswith("? ") or "select an option" in l_str or "[y/n]" in l_str or "menu" in l_str:
                raise BridgeCapabilityGap(f"Session in menu/prompt state: {line}")

        # Composer must be an empty prompt: no draft text after the glyph, no ambiguous text before it
        if not composer_is_empty(last_line):
            raise BridgeCapabilityGap(f"Composer not empty or invalid prompt: {last_line}")

    def forward_hub_to_native(self, hub_msg: dict[str, Any], native_tag: str | None = None) -> str:
        """Queue a hub message into the native mailbox of its recipient tag.

        `native_tag` is the registered tag whose hub inbox returned the message (the hub resolves
        tag, project:tag and unique role aliases); without it the tag comes from to_actor. A
        to_actor that names a different native tag, or has no native mailbox, is refused before
        any native effect.
        """
        mapped = native_tag_for_address(hub_msg.get("to_actor"))
        if native_tag is None:
            native_tag = mapped
        elif mapped is not None and mapped != native_tag:
            raise BridgeCapabilityGap(
                f"Hub message {hub_msg.get('id')} is addressed to '{hub_msg.get('to_actor')}', not native tag '{native_tag}'"
            )
        if not native_tag or native_tag_for_address(native_tag) != native_tag:
            raise BridgeCapabilityGap(
                f"Hub message {hub_msg.get('id')} has no native mailbox tag for '{hub_msg.get('to_actor')}'"
            )
        return self._forward_hub_to_native(hub_msg, native_tag)

    def _forward_hub_to_native(self, hub_msg: dict[str, Any], tag: str) -> str:
        with self._receipts_lock():
            self._sync_receipts_with_lock()

            hub_id = hub_msg["id"]
            if "hub_to_native" not in self.receipts:
                self.receipts["hub_to_native"] = {}
            if "hub_messages" not in self.receipts:
                self.receipts["hub_messages"] = {}
            if "native_to_hub" not in self.receipts:
                self.receipts["native_to_hub"] = {}
            if "acked_hub_messages" not in self.receipts:
                self.receipts["acked_hub_messages"] = []
            if "ambiguous_sends" not in self.receipts:
                self.receipts["ambiguous_sends"] = {}
            if "delivers" not in self.receipts:
                self.receipts["delivers"] = {}

            target = hub_msg["to_actor"]
            body = hub_msg["body"]
            kind = hub_msg.get("kind", "note")

            # 1. Queue once: if already successfully queued/sent, retry with existing native ID
            if hub_id in self.receipts["hub_to_native"]:
                native_id = self.receipts["hub_to_native"][hub_id]
            elif hub_id in self.receipts["ambiguous_sends"]:
                amb = self.receipts["ambiguous_sends"][hub_id]
                if not isinstance(amb, dict):
                    raise BridgeCapabilityGap(
                        f"Hub message {hub_id} has a malformed ambiguous send record; "
                        f"refusing to resend to prevent duplicate native effects"
                    )
                raise BridgeCapabilityGap(
                    f"Hub message {hub_id} has a prior ambiguous send (status={amb.get('status', 'unknown')}, native_id={amb.get('native_id')}); "
                    f"refusing to resend to prevent duplicate native effects: {amb.get('reason')}"
                )
            else:
                # Persist send intent BEFORE execution and hold ambiguous execution outcomes durably
                self.receipts.setdefault("ambiguous_sends", {})[hub_id] = {
                    "native_id": None,
                    "status": "intent",
                    "reason": "Send intent recorded before execution",
                    "created_at": int(time.time()),
                }
                self._save_receipts()

                cmd = [self.aplexer_bin, "message", "send", "--queue", "--to", tag, "--kind", kind, "--json", body]
                sub_env = self._get_subprocess_env()
                if self.service_binding:
                    sub_env["APLEXER_SESSION_ID"] = str(self.service_binding.session_id)

                try:
                    res = subprocess.run(
                        cmd,
                        cwd=self.workspace,
                        capture_output=True,
                        text=True,
                        timeout=180,
                        env=sub_env,
                    )
                except (FileNotFoundError, PermissionError) as e:
                    # Preserve unsuccessful before-execution error distinction only with real evidence
                    self.receipts.get("ambiguous_sends", {}).pop(hub_id, None)
                    self._save_receipts()
                    raise RuntimeError(f"Native send failed before execution: {e}") from e
                except subprocess.TimeoutExpired as e:
                    self.receipts["ambiguous_sends"][hub_id] = {
                        "native_id": None,
                        "status": "timeout",
                        "reason": f"Native send timed out after 180s: {e}",
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise BridgeCapabilityGap(f"Native send timed out; send outcome uncertain: {e}") from e
                except Exception as e:
                    self.receipts["ambiguous_sends"][hub_id] = {
                        "native_id": None,
                        "status": "uncertain",
                        "reason": f"Native send execution failed: {e}",
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise BridgeCapabilityGap(f"Native send execution failed; send outcome uncertain: {e}") from e

                if res.returncode != 0:
                    self.receipts["ambiguous_sends"][hub_id] = {
                        "native_id": None,
                        "status": "uncertain",
                        "returncode": res.returncode,
                        "stderr": res.stderr[:500],
                        "reason": f"Native send failed (exit {res.returncode}): {res.stderr[:500]}",
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise BridgeCapabilityGap(f"Native send failed (exit {res.returncode}): {res.stderr[:500]}")

                try:
                    native_msg = json.loads(res.stdout)
                    native_id = native_msg["id"]
                except Exception as e:
                    self.receipts["ambiguous_sends"][hub_id] = {
                        "native_id": None,
                        "status": "uncertain",
                        "reason": f"Failed to parse native send output: {e}",
                        "stdout": res.stdout[:500],
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise BridgeCapabilityGap(f"Failed to parse native send output: {e}") from e

                # If sender was resolved by native CLI and we did not have a prior binding, attempt qualification for allowed bridge tags
                msg_from = native_msg.get("from") if isinstance(native_msg, dict) else None
                if isinstance(msg_from, dict) and not self.service_binding and not msg_from.get("external"):
                    sid = msg_from.get("session_id")
                    stag = msg_from.get("tag")
                    allowed_tags = [self.service_tag] if self.service_tag else ["hub-bridge", "agent-hub-bridge"]
                    if sid and stag in allowed_tags:
                        disc = qualify_native_service_binding(
                            self.canonical_workspace,
                            state_dir=get_authoritative_state_dir(self.state_dir),
                            runtime_dir=get_authoritative_runtime_dir(self.runtime_dir),
                            service_tag=stag,
                            service_session_id=str(sid),
                        )
                        if disc:
                            self.service_binding = disc

                # Validate response provenance matches actual qualified own bridge sender
                try:
                    validate_native_send_envelope(native_msg, self.service_binding, self.canonical_workspace)
                except BridgeCapabilityGap as gap:
                    # Record ambiguous send evidence on disk so we do not erase it or resend duplicates
                    self.receipts["ambiguous_sends"][hub_id] = {
                        "native_id": native_id,
                        "status": "uncertain",
                        "reason": str(gap),
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise

                # Persist receipt envelope and nativeID atomically mode 0600 BEFORE deliver
                self.receipts["hub_to_native"][hub_id] = native_id
                self.receipts.get("ambiguous_sends", {}).pop(hub_id, None)
                msg_envelope_info = {
                    "from_actor": hub_msg.get("from_actor"),  # Exact original actor
                    "to_actor": target,
                    "hub_id": hub_id,
                    "thread_id": hub_msg.get("thread_id"),
                    "kind": kind,
                    "native_tag": tag,
                }
                if self.service_binding:
                    msg_envelope_info["service_tag"] = self.service_binding.tag
                    msg_envelope_info["service_session_id"] = str(self.service_binding.session_id)

                if isinstance(native_msg, dict) and isinstance(native_msg.get("to"), dict):
                    to_sid = native_msg["to"].get("session_id")
                    if to_sid:
                        msg_envelope_info["recipient_session_id"] = str(to_sid)

                self.receipts["hub_messages"][native_id] = msg_envelope_info
                self._save_receipts()

            # 2. Sync reply delivery with durable intent / uncertainty ledger
            if kind == "sync_reply":
                msg_envelope_info = self.receipts.get("hub_messages", {}).get(native_id, {})
                deliv_state = self.receipts.get("delivers", {}).get(native_id)
                if deliv_state:
                    if not isinstance(deliv_state, dict):
                        raise BridgeCapabilityGap(
                            f"Hub message {hub_id} (native_id={native_id}) has malformed delivery state; "
                            f"refusing redelivery to prevent duplicate PTY writes"
                        )
                    d_status = deliv_state.get("status")
                    if d_status == "delivered":
                        # Already successfully delivered: zero additional writes
                        return native_id
                    elif d_status in ("intent", "uncertain", "timeout", "failed"):
                        # Reconcile ONLY genuine terminal delivery evidence (recipient positive cursor ACK)
                        if self._is_native_id_positively_acked(native_id, target, msg_envelope_info):
                            deliv_state["status"] = "delivered"
                            deliv_state["reconciled_at"] = int(time.time())
                            self._save_receipts()
                            return native_id
                        # Native popped message alone not proof of delivery; any unknown result stays held!
                        raise BridgeCapabilityGap(
                            f"Hub message {hub_id} (native_id={native_id}) has prior uncertain/intent delivery state ({d_status}) "
                            f"without genuine terminal delivery evidence; refusing redelivery to prevent duplicate PTY writes"
                        )
                    else:
                        # Unknown / corrupt / version-skew delivery state must hold with zero replay; never silently reset or overwrite
                        raise BridgeCapabilityGap(
                            f"Hub message {hub_id} (native_id={native_id}) has unfamiliar/unknown delivery state ({d_status}); "
                            f"refusing redelivery to prevent duplicate PTY writes"
                        )

                # Screen capture composer check: two fresh observations then immediate final check
                self._check_idle_and_empty_composer(tag)
                time.sleep(0.05)
                self._check_idle_and_empty_composer(tag)
                self._check_idle_and_empty_composer(tag)

                # Record durable intent BEFORE possible effect
                self.receipts.setdefault("delivers", {})[native_id] = {
                    "hub_id": hub_id,
                    "status": "intent",
                    "attempted_at": int(time.time()),
                }
                self._save_receipts()

                sub_env = self._get_subprocess_env()
                deliv_cmd = [self.aplexer_bin, "message", "deliver", native_id, "--workspace", self.workspace]
                try:
                    deliv_res = subprocess.run(
                        deliv_cmd, cwd=self.workspace, capture_output=True, text=True, timeout=30, env=sub_env
                    )
                except subprocess.TimeoutExpired as e:
                    self.receipts["delivers"][native_id]["status"] = "timeout"
                    self.receipts["delivers"][native_id]["reason"] = f"Delivery timed out after 30s: {e}"
                    self.receipts.setdefault("ambiguous_sends", {})[hub_id] = {
                        "native_id": native_id,
                        "reason": f"Delivery timed out after 30s: {e}",
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise BridgeCapabilityGap(f"Native delivery timed out; delivery state uncertain, PTY effect held: {e}")
                except Exception as e:
                    self.receipts["delivers"][native_id]["status"] = "uncertain"
                    self.receipts["delivers"][native_id]["reason"] = f"Delivery execution failed: {e}"
                    self.receipts.setdefault("ambiguous_sends", {})[hub_id] = {
                        "native_id": native_id,
                        "reason": f"Delivery execution failed: {e}",
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise BridgeCapabilityGap(f"Native delivery execution failed; delivery state uncertain: {e}")

                if deliv_res.returncode != 0:
                    self.receipts["delivers"][native_id]["status"] = "uncertain"
                    self.receipts["delivers"][native_id]["returncode"] = deliv_res.returncode
                    self.receipts["delivers"][native_id]["stderr"] = deliv_res.stderr[:200]
                    self.receipts.setdefault("ambiguous_sends", {})[hub_id] = {
                        "native_id": native_id,
                        "reason": f"Native delivery refused (exit {deliv_res.returncode}): {deliv_res.stderr[:200]}",
                        "created_at": int(time.time()),
                    }
                    self._save_receipts()
                    raise BridgeCapabilityGap(f"Native delivery refused: {deliv_res.stderr[:200]}")

                self.receipts["delivers"][native_id]["status"] = "delivered"
                self.receipts["delivers"][native_id]["delivered_at"] = int(time.time())
                self._save_receipts()

            return native_id

    def sync_native_to_hub(self) -> dict[str, list[dict[str, Any]]]:
        """Native recipient ACKs and replies -> hub.

        Returns {"errors": [...], "held": [...]}: hub-side ack/reply failures and unreadable native
        logs are errors the caller must surface; replies refused on provenance are held. Neither is
        retried here as a new native effect.
        """
        outcome: dict[str, list[dict[str, Any]]] = {"errors": [], "held": []}
        with self._receipts_lock():
            self._sync_receipts_with_lock()

            if "hub_to_native" not in self.receipts:
                self.receipts["hub_to_native"] = {}
            if "hub_messages" not in self.receipts:
                self.receipts["hub_messages"] = {}
            if "native_to_hub" not in self.receipts:
                self.receipts["native_to_hub"] = {}
            if "acked_hub_messages" not in self.receipts:
                self.receipts["acked_hub_messages"] = []
            if "ambiguous_sends" not in self.receipts:
                self.receipts["ambiguous_sends"] = {}
            if "delivers" not in self.receipts:
                self.receipts["delivers"] = {}

            native_to_hub_map = {v: k for k, v in self.receipts["hub_to_native"].items()}

            # 1. Check positive ACKs for forwarded hub messages
            for native_id, hub_id in list(native_to_hub_map.items()):
                hub_msg_info = self.receipts.get("hub_messages", {}).get(native_id, {})
                to_actor = hub_msg_info.get("to_actor")
                if not to_actor:
                    continue

                if self._is_native_id_positively_acked(native_id, to_actor, hub_msg_info):
                    # Reconcile delivery state if present
                    if native_id in self.receipts.get("delivers", {}):
                        self.receipts["delivers"][native_id]["status"] = "delivered"
                        self.receipts["delivers"][native_id]["reconciled_at"] = int(time.time())
                        self._save_receipts()

                    if hub_id not in self.receipts["acked_hub_messages"]:
                        try:
                            bus.ack(hub_id)
                            self.receipts["acked_hub_messages"].append(hub_id)
                            self._save_receipts()
                        except Exception as e:
                            outcome["errors"].append({"hub_id": hub_id, "error": f"hub ack failed: {e}"[:300]})

            # 2. Sync replies from read-only aplexer message log
            cmd = [self.aplexer_bin, "message", "log", "--json", "--workspace", self.workspace]
            sub_env = self._get_subprocess_env()
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=sub_env)
            except Exception as e:
                outcome["errors"].append({"error": f"aplexer message log failed: {e}"[:300]})
                return outcome

            if res.returncode != 0:
                outcome["errors"].append({"error": f"aplexer message log failed (exit {res.returncode}): {res.stderr[:200]}"})
                return outcome

            try:
                log_entries = json.loads(res.stdout)
            except Exception as e:
                outcome["errors"].append({"error": f"aplexer message log is not JSON: {e}"[:300]})
                return outcome
            if not isinstance(log_entries, list):
                outcome["errors"].append({"error": "aplexer message log is not a JSON list"})
                return outcome

            for msg in log_entries:
                if not isinstance(msg, dict):
                    continue
                native_id = msg.get("id")
                if not native_id:
                    continue
                if native_id in self.receipts.get("native_to_hub", {}):
                    continue

                kind = normalize_native_kind(msg.get("kind", "reply"))
                reply_to = msg.get("reply_to")

                # If it's a native reply to something we forwarded
                if reply_to and reply_to in native_to_hub_map:
                    hub_reply_to = native_to_hub_map[reply_to]
                    original = self.receipts.get("hub_messages", {}).get(reply_to, {})
                    to_actor = original.get("from_actor")
                    sender_actor = original.get("to_actor")
                    thread_id = original.get("thread_id")
                    if not to_actor or not sender_actor:
                        outcome["errors"].append({"native_id": native_id, "error": "cannot bridge reply: missing original actors"})
                        continue

                    # Provenance is checked against the exact native tag the message was queued to,
                    # not the hub address form (project:tag or role:* are not native tags).
                    native_tag = original.get("native_tag") or native_tag_for_address(sender_actor)
                    if not native_tag:
                        outcome["errors"].append({"native_id": native_id, "error": f"cannot bridge reply: no native tag for '{sender_actor}'"[:300]})
                        continue
                    valid, reason = validate_native_reply_provenance(
                        msg,
                        dict(original, to_actor=native_tag),
                        self.canonical_workspace,
                        self.service_binding,
                        get_authoritative_state_dir(self.state_dir),
                        get_authoritative_runtime_dir(self.runtime_dir),
                    )
                    if not valid:
                        outcome["held"].append({"native_id": native_id, "reason": f"reply refused: {reason}"[:300]})
                        continue

                    body = msg.get("body", "")
                    try:
                        res_hub = bus.reply(
                            hub_reply_to,
                            sender_actor,
                            to_actor,
                            kind,
                            body,
                            idempotency_key=native_id,
                            thread_id=thread_id
                        )
                        hub_rep_id = res_hub.get("id") if isinstance(res_hub, dict) else str(res_hub)
                        self.receipts["native_to_hub"][native_id] = hub_rep_id
                        self._save_receipts()
                    except Exception as e:
                        outcome["errors"].append({"native_id": native_id, "hub_id": hub_reply_to, "error": f"hub reply failed: {e}"[:300]})
        return outcome


def registered_bridge_targets(actors: Any, only_tags: list[str] | None = None) -> list[tuple[str, str]]:
    """(hub inbox address, native tag) pairs for the agents the hub registers for this credential.

    `actors` is the hub's /api/bus/actors answer (agents on this host plus machine:<id>).
    `only_tags` narrows the set; a requested tag the hub does not register is refused, so the
    bridge never polls or writes a mailbox it was not given. A selected native tag shared
    by multiple qualified project addresses is refused before any bridge effects.
    """
    targets: dict[str, str] = {}
    qualified: dict[str, set[str]] = {}
    for address in actors if isinstance(actors, list) else []:
        tag = native_tag_for_address(address)
        if not tag:
            continue
        if ":" in address:
            qualified.setdefault(tag, set()).add(address)
        # Prefer project:tag over its bare alias; reject cross-project collisions below.
        if tag not in targets or ":" in address:
            targets[tag] = address
    if only_tags is not None:
        unknown = sorted(set(only_tags) - set(targets))
        if unknown:
            raise BridgeCapabilityGap(f"Tags not registered on the hub for this credential: {', '.join(unknown)}")
        targets = {t: targets[t] for t in only_tags}
    ambiguous = sorted(tag for tag in targets if len(qualified.get(tag, set())) > 1)
    if ambiguous:
        raise BridgeCapabilityGap(f"ambiguous native tags registered across projects: {', '.join(ambiguous)}")
    return sorted((address, tag) for tag, address in targets.items())


def _bridge_inbox_message(bridge: MailboxBridge, msg: dict[str, Any], tag: str, summary: dict[str, Any]) -> bool:
    """Forward one hub inbox message; True when it is settled (hub-acked, or forwarded now or before)."""
    hub_id = msg["id"]
    if msg.get("acked_at") or hub_id in bridge.receipts.get("acked_hub_messages", []):
        summary["acked_skipped"] += 1
        return True
    if hub_id in bridge.receipts.get("hub_to_native", {}) and msg.get("kind") != "sync_reply":
        summary["already_forwarded"] += 1
        return True
    try:
        native_id = bridge.forward_hub_to_native(msg, native_tag=tag)
    except BridgeCapabilityGap as e:
        summary["held"].append({"hub_id": hub_id, "tag": tag, "reason": str(e)[:300]})
        return False
    except Exception as e:
        summary["errors"].append({"hub_id": hub_id, "tag": tag, "error": str(e)[:300]})
        return False
    summary["forwarded"].append({"hub_id": hub_id, "native_id": native_id, "tag": tag})
    return True


def poll_once(
    bridge: MailboxBridge,
    targets: list[tuple[str, str]],
    limit: int = 50,
    max_pages: int = 10,
) -> dict[str, Any]:
    """One bounded bridge cycle: hub inbox -> native mailbox for each target, then native -> hub.

    Held rows are durably retained before any native effect. A separate scan continuation
    crosses bounded windows; the inbox cursor keeps the newest-millisecond fence. At the end
    of a scan, polling returns to that fence to catch later writes in the same millisecond.
    Pending retries are bounded and survive restarts. Capacity exhaustion stops scanning
    without discarding a row. Only delivery or explicit message expiry clears pending rows.

    Reading marks hub delivery; only the native recipient's positive ACK authorizes hub ACK.
    """
    summary: dict[str, Any] = {"forwarded": [], "already_forwarded": 0, "acked_skipped": 0, "held": [], "errors": []}
    for address, tag in targets:
        retried = set()
        try:
            for hub_id, msg in bridge.pending_inbox(address).items():
                retried.add(hub_id)
                expiry = msg.get("expires_at")
                if isinstance(expiry, int) and not isinstance(expiry, bool) and expiry <= int(time.time() * 1000):
                    bridge.forget_inbox(address, hub_id)
                elif _bridge_inbox_message(bridge, msg, tag, summary):
                    bridge.forget_inbox(address, hub_id)
        except Exception as e:
            summary["errors"].append({"address": address, "error": f"pending inbox failed: {e}"[:300]})
            continue
        cursor = bridge.inbox_scan(address)
        scanned: list[tuple[str, Any]] = []
        newest = None
        exhausted = True
        for _ in range(max_pages):
            try:
                page = bus.inbox(address, limit=limit, cursor=cursor)
                if not isinstance(page, list):
                    raise ValueError("inbox did not return a list")
            except Exception as e:
                summary["errors"].append({"address": address, "error": f"inbox failed: {e}"[:300]})
                exhausted = False
                break
            if not page:
                bridge.save_inbox_scan(address, None)
                exhausted = False
                break
            stopped = False
            for msg in page:
                if not isinstance(msg, dict) or not msg.get("id"):
                    summary["errors"].append({"address": address, "error": "inbox row without an id"})
                    stopped = True
                    break
                try:
                    if msg["id"] not in retried:
                        # Save before the effect, including uncertain sends; never evict held rows.
                        bridge.remember_inbox(address, msg)
                        if _bridge_inbox_message(bridge, msg, tag, summary):
                            bridge.forget_inbox(address, msg["id"])
                    created_at = msg.get("created_at")
                    if isinstance(created_at, int) and (newest is None or created_at > newest):
                        newest = created_at
                    scanned.append((msg["id"], created_at))
                    cursor = msg["id"]
                except Exception as e:
                    summary["errors"].append({"address": address, "error": f"pending inbox failed: {e}"[:300]})
                    stopped = True
                    break
            safe = [mid for mid, ts in scanned if isinstance(ts, int) and newest is not None and ts < newest]
            if safe:
                bridge.advance_inbox_cursor(address, safe[-1])
            # An interrupted page resumes after the last durably retained row.
            bridge.save_inbox_scan(address, cursor if stopped or len(page) >= limit else None)
            if stopped or len(page) < limit:
                exhausted = False
                break
        if exhausted:
            summary["errors"].append({
                "address": address,
                "error": f"inbox window exhausted: {max_pages} pages of {limit} read without reaching the end; "
                         f"the rest is read from the durable scan next cycle",
            })
        pending_count = len(bridge.pending_inbox(address))
        if pending_count:
            summary["errors"].append({"address": address, "error": f"inbox held: {pending_count} messages retained for retry"})
    try:
        synced = bridge.sync_native_to_hub() or {}
        summary["errors"].extend(synced.get("errors", []))
        summary["held"].extend(synced.get("held", []))
    except Exception as e:
        summary["errors"].append({"error": f"native to hub sync failed: {e}"[:300]})
    return summary
