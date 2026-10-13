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
import sys
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
            return data
        return {
            "hub_to_native": {},
            "native_to_hub": {},
            "hub_messages": {},
            "acked_hub_messages": [],
            "ambiguous_sends": {},
            "delivers": {},
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
        target_tag = (to_actor[6:] if to_actor.startswith("agent:") else to_actor).strip()
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

        # Composer must end with prompt character and have no draft text after it
        if not (last_line.endswith("❯") or last_line.endswith("$") or last_line.endswith("#") or last_line.endswith(">")):
            raise BridgeCapabilityGap(f"Composer not empty or invalid prompt: {last_line}")

    def forward_hub_to_native(self, hub_msg: dict[str, Any]) -> str:
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
            tag = target[6:] if target.startswith("agent:") else target
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

    def sync_native_to_hub(self) -> None:
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
                            print(f"Failed to ack hub message {hub_id}: {e}", file=sys.stderr)

            # 2. Sync replies from read-only aplexer message log
            cmd = [self.aplexer_bin, "message", "log", "--json", "--workspace", self.workspace]
            sub_env = self._get_subprocess_env()
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=sub_env)
            except Exception as e:
                print(f"Failed to run aplexer message log: {e}", file=sys.stderr)
                return

            if res.returncode != 0:
                print(f"Failed to run aplexer message log: {res.stderr[:200]}", file=sys.stderr)
                return

            try:
                log_entries = json.loads(res.stdout)
                if not isinstance(log_entries, list):
                    return
            except Exception as e:
                print(f"Failed to parse aplexer message log: {e}", file=sys.stderr)
                return

            for msg in log_entries:
                if not isinstance(msg, dict):
                    continue
                native_id = msg.get("id")
                if not native_id:
                    continue
                if native_id in self.receipts.get("native_to_hub", {}):
                    continue

                kind = msg.get("kind", "reply")
                reply_to = msg.get("reply_to")

                # If it's a native reply to something we forwarded
                if reply_to and reply_to in native_to_hub_map:
                    hub_reply_to = native_to_hub_map[reply_to]
                    original = self.receipts.get("hub_messages", {}).get(reply_to, {})
                    to_actor = original.get("from_actor")
                    sender_actor = original.get("to_actor")
                    thread_id = original.get("thread_id")
                    if not to_actor or not sender_actor:
                        print(f"Cannot bridge reply {native_id}: missing original actors", file=sys.stderr)
                        continue

                    valid, reason = validate_native_reply_provenance(
                        msg,
                        original,
                        self.canonical_workspace,
                        self.service_binding,
                        get_authoritative_state_dir(self.state_dir),
                        get_authoritative_runtime_dir(self.runtime_dir),
                    )
                    if not valid:
                        print(f"Skipping reply {native_id}: {reason}", file=sys.stderr)
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
                        print(f"Failed to bridge reply {native_id} to hub: {e}", file=sys.stderr)
