"""Native Aplexer Mailbox Bridge Helper (docs/SPEC.md §7.5).

Provides native service sender binding qualification, response envelope
provenance validation, recipient reply verification adhering strictly to
read-only Rust aplexer messaging contracts (envelope.rs, message_routing.rs),
and safe cursor state parsing.

Upstream Architectural Note (SPEC §11 Gap):
-------------------------------------------
Under native aplexer messaging:
- `MessageFrom::anonymous()` emits `{"external": true}` with no tag or session_id.
- `bin/aplexer/message_routing.rs:reply_target` explicitly requires `original.from.tag`:
    let original_tag = original.from.tag.as_ref().ok_or_else(|| {
        anyhow!("original message {} was sent anonymously (no sender tag)", original.id)
    })?;
- When an unbound CLI runs `aplexer message send`, it emits an anonymous envelope.
  Native recipients are strictly unable to reply to anonymous messages.
- Therefore, a genuine native service sender binding in authoritative session
  records is required for bidirectional bridging. If unavailable in deployed
  runtimes, the bridge reports BridgeCapabilityGap truthfully and refuses false
  bidirectional roundtrip/ACK receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import private_fs


MAX_MAILBOX_STATE_BYTES = 1024 * 1024  # 1 MiB boundary for metadata files


class BridgeCapabilityGap(Exception):
    """Raised when a bridge operation cannot be performed safely or is refused."""
    pass


@dataclass(frozen=True)
class NativeServiceBinding:
    """Authoritative native session identity for an owned bridge service."""
    session_id: str
    tag: str
    workspace: str
    engine: str = "hub-bridge"
    profile: str | None = None
    created_at_ms: int = 0


def canonicalize_workspace(workspace_path: str) -> str:
    """Canonicalize workspace path matching aplexer's canonical_workspace."""
    if os.name == 'nt':
        from .win32_fs import canonical_workspace
        return canonical_workspace(workspace_path)
    abs_path = os.path.abspath(workspace_path)
    try:
        return os.path.realpath(abs_path)
    except Exception:
        return abs_path


def get_workspace_key(canonical_workspace_path: str) -> str:
    """Compute mailbox directory key: first 128 bits of SHA-256 (32 hex characters)."""
    return hashlib.sha256(canonical_workspace_path.encode("utf-8")).hexdigest()[:32]


def qualify_native_service_binding(
    canonical_workspace: str,
    state_dir: str | None = None,
    runtime_dir: str | None = None,
    service_tag: str | None = None,
    service_session_id: str | None = None,
) -> NativeServiceBinding | None:
    """Qualify genuine native service sender binding from authoritative session records.

    Verifies:
    1. Session record file exists in state_dir/sessions/<id>/session.json or runtime_dir.
    2. Parsed JSON record has matching session ID and canonical workspace.
    3. If service_tag is specified, record tag matches.
    4. Never adopts another agent's recipient identity or a forged in-memory flag.
    5. Returns None honestly if no genuine binding exists in deployed storage.
    """
    sessions_dirs: list[str] = []
    if runtime_dir and os.path.isdir(os.path.join(runtime_dir, "sessions")):
        s_dir = os.path.join(runtime_dir, "sessions")
        if not os.path.islink(s_dir):
            sessions_dirs.append(s_dir)
    if state_dir and os.path.isdir(os.path.join(state_dir, "sessions")):
        s_dir = os.path.join(state_dir, "sessions")
        if not os.path.islink(s_dir):
            sessions_dirs.append(s_dir)

    if not sessions_dirs:
        return None

    allowed_service_tags = [service_tag] if service_tag else ["hub-bridge", "agent-hub-bridge"]

    # Priority 1: Explicit service_session_id
    if service_session_id:
        try:
            norm_sid = str(uuid.UUID(str(service_session_id)))
        except (ValueError, TypeError):
            return None

        for sdir in sessions_dirs:
            entry_dir = os.path.join(sdir, norm_sid)
            if os.path.islink(entry_dir) or not os.path.isdir(entry_dir):
                continue
            rec_file = os.path.join(entry_dir, "session.json")
            if os.path.isfile(rec_file) and not os.path.islink(rec_file):
                try:
                    rec = json.loads(private_fs.read_bytes(rec_file, MAX_MAILBOX_STATE_BYTES, private=False))
                    if isinstance(rec, dict):
                        rec_id = str(rec.get("id", ""))
                        rec_ws = str(rec.get("workspace", ""))
                        rec_tag = str(rec.get("tag", ""))
                        if (
                            rec_id.lower() == norm_sid.lower()
                            and canonicalize_workspace(rec_ws) == canonical_workspace
                            and (rec_tag in allowed_service_tags)
                            and rec_tag
                        ):
                            return NativeServiceBinding(
                                session_id=rec_id,
                                tag=rec_tag,
                                workspace=canonical_workspace,
                                engine=str(rec.get("engine", "hub-bridge")),
                                profile=rec.get("profile"),
                                created_at_ms=int(rec.get("created_at_ms", 0)),
                            )
                except Exception:
                    pass
        return None

    # Priority 2: Ambient APLEXER_SESSION_ID in process environment
    env_sid = os.environ.get("APLEXER_SESSION_ID")
    if env_sid:
        try:
            norm_sid = str(uuid.UUID(str(env_sid).strip()))
            for sdir in sessions_dirs:
                entry_dir = os.path.join(sdir, norm_sid)
                if os.path.islink(entry_dir) or not os.path.isdir(entry_dir):
                    continue
                rec_file = os.path.join(entry_dir, "session.json")
                if os.path.isfile(rec_file) and not os.path.islink(rec_file):
                    rec = json.loads(private_fs.read_bytes(rec_file, MAX_MAILBOX_STATE_BYTES, private=False))
                    if isinstance(rec, dict):
                        rec_id = str(rec.get("id", ""))
                        rec_ws = str(rec.get("workspace", ""))
                        rec_tag = str(rec.get("tag", ""))
                        if (
                            rec_id.lower() == norm_sid.lower()
                            and canonicalize_workspace(rec_ws) == canonical_workspace
                            and (rec_tag in allowed_service_tags)
                            and rec_tag
                        ):
                            return NativeServiceBinding(
                                session_id=rec_id,
                                tag=rec_tag,
                                workspace=canonical_workspace,
                                engine=str(rec.get("engine", "hub-bridge")),
                                profile=rec.get("profile"),
                                created_at_ms=int(rec.get("created_at_ms", 0)),
                            )
        except Exception:
            pass

    # Priority 3: Search authoritative sessions for dedicated bridge service tag
    target_tags = allowed_service_tags
    matching: list[NativeServiceBinding] = []

    for sdir in sessions_dirs:
        try:
            for entry in os.listdir(sdir):
                entry_dir = os.path.join(sdir, entry)
                if not os.path.isdir(entry_dir) or os.path.islink(entry_dir):
                    continue
                rec_file = os.path.join(entry_dir, "session.json")
                if os.path.isfile(rec_file) and not os.path.islink(rec_file):
                    try:
                        rec = json.loads(private_fs.read_bytes(rec_file, MAX_MAILBOX_STATE_BYTES, private=False))
                        if isinstance(rec, dict):
                            rec_id = str(rec.get("id", ""))
                            rec_ws = str(rec.get("workspace", ""))
                            rec_tag = str(rec.get("tag", ""))
                            if (
                                rec_tag in target_tags
                                and canonicalize_workspace(rec_ws) == canonical_workspace
                                and rec_id
                            ):
                                matching.append(
                                    NativeServiceBinding(
                                        session_id=rec_id,
                                        tag=rec_tag,
                                        workspace=canonical_workspace,
                                        engine=str(rec.get("engine", "hub-bridge")),
                                        profile=rec.get("profile"),
                                        created_at_ms=int(rec.get("created_at_ms", 0)),
                                    )
                                )
                    except Exception:
                        continue
        except Exception:
            continue

    if matching:
        # Sort by created_at_ms descending (latest record)
        matching.sort(key=lambda b: -b.created_at_ms)
        return matching[0]

    return None


def validate_native_send_envelope(
    native_msg: dict[str, Any],
    service_binding: NativeServiceBinding | None,
    canonical_workspace: str,
) -> None:
    """Validate response provenance of native send against qualified own bridge sender.

    Refuses anonymous (from.external=true), absent service binding, or mismatched senders
    with BridgeCapabilityGap.
    """
    if not isinstance(native_msg, dict):
        raise BridgeCapabilityGap("Native send output is not a valid JSON envelope dict")

    if service_binding is None:
        raise BridgeCapabilityGap(
            "Bridge own session binding is absent or unqualified (anonymous sender); external:false is not positive authentication"
        )

    # 1. Workspace check (if workspace present in envelope)
    ws = native_msg.get("workspace")
    if ws and canonicalize_workspace(str(ws)) != canonical_workspace:
        raise BridgeCapabilityGap(
            f"Native send envelope workspace mismatch: {ws} != {canonical_workspace}"
        )

    # 2. From provenance check
    msg_from = native_msg.get("from")
    if not isinstance(msg_from, dict):
        raise BridgeCapabilityGap("Native send envelope missing 'from' object")

    if msg_from.get("external") is True:
        raise BridgeCapabilityGap(
            "Native send envelope emitted external:true (anonymous sender). "
            "Native recipients will be unable to reply per Rust reply_target contract."
        )

    sender_tag = msg_from.get("tag")
    sender_sid = msg_from.get("session_id")

    if not sender_tag or not sender_sid:
        raise BridgeCapabilityGap(
            "Native send envelope 'from' missing tag or session_id for positive authentication"
        )

    if sender_tag != service_binding.tag:
        raise BridgeCapabilityGap(
            f"Native send envelope sender tag '{sender_tag}' does not match bridge service '{service_binding.tag}'"
        )
    if str(sender_sid).lower() != str(service_binding.session_id).lower():
        raise BridgeCapabilityGap(
            f"Native send envelope sender session '{sender_sid}' does not match bridge service '{service_binding.session_id}'"
        )
    from_ws = msg_from.get("workspace")
    if from_ws and canonicalize_workspace(str(from_ws)) != canonical_workspace:
        raise BridgeCapabilityGap(
            f"Native send envelope 'from.workspace' mismatch: {from_ws} != {canonical_workspace}"
        )



def validate_native_reply_provenance(
    reply_msg: dict[str, Any],
    original_hub_info: dict[str, Any],
    canonical_workspace: str,
    service_binding: NativeServiceBinding | None,
    state_dir: str | None,
    runtime_dir: str | None,
) -> tuple[bool, str]:
    """Validate reply message from native message log against original recipient session.

    Matches exact two original parties, verifies sender provenance exists in
    authoritative session storage, and rejects foreign workspaces, external
    senders, or missing provenance.
    """
    if not isinstance(reply_msg, dict):
        return False, "reply message is not a dict"

    # 1. Explicit from object required
    msg_from = reply_msg.get("from")
    if not isinstance(msg_from, dict):
        return False, "missing or null 'from' object"

    if msg_from.get("external") is True:
        return False, "reply from external sender cannot be bridged as agent reply"

    replier_tag = msg_from.get("tag")
    replier_sid = msg_from.get("session_id")
    if not replier_tag or not replier_sid:
        return False, "reply 'from' missing tag or session_id"

    # 2. Workspace validation
    msg_ws = reply_msg.get("workspace") or msg_from.get("workspace")
    if msg_ws and canonicalize_workspace(str(msg_ws)) != canonical_workspace:
        return False, f"reply workspace {msg_ws} does not match {canonical_workspace}"

    # 3. Exact original recipient match
    to_actor = original_hub_info.get("to_actor", "")
    expected_tag = to_actor[6:] if to_actor.startswith("agent:") else to_actor
    if replier_tag != expected_tag:
        return False, f"replier tag '{replier_tag}' does not match expected '{expected_tag}'"

    expected_sid = original_hub_info.get("recipient_session_id")
    if expected_sid:
        if str(replier_sid).lower() != str(expected_sid).lower():
            return False, f"replier session '{replier_sid}' does not match expected '{expected_sid}'"

    # 4. Authoritative session record verification
    sessions_dirs: list[str] = []
    if runtime_dir and os.path.isdir(os.path.join(runtime_dir, "sessions")):
        s_dir = os.path.join(runtime_dir, "sessions")
        if not os.path.islink(s_dir):
            sessions_dirs.append(s_dir)
    if state_dir and os.path.isdir(os.path.join(state_dir, "sessions")):
        s_dir = os.path.join(state_dir, "sessions")
        if not os.path.islink(s_dir):
            sessions_dirs.append(s_dir)

    if not sessions_dirs:
        return False, "authoritative sessions directory unavailable (UNKNOWN); deny reply provenance"

    session_verified = False
    for sdir in sessions_dirs:
        entry_dir = os.path.join(sdir, str(replier_sid))
        if os.path.islink(entry_dir) or not os.path.isdir(entry_dir):
            continue
        rec_file = os.path.join(entry_dir, "session.json")
        if os.path.isfile(rec_file) and not os.path.islink(rec_file):
            try:
                rec = json.loads(private_fs.read_bytes(rec_file, MAX_MAILBOX_STATE_BYTES, private=False))
                if isinstance(rec, dict):
                    if (
                        str(rec.get("id", "")).lower() == str(replier_sid).lower()
                        and rec.get("tag") == expected_tag
                        and canonicalize_workspace(str(rec.get("workspace", ""))) == canonical_workspace
                    ):
                        session_verified = True
                        break
            except Exception:
                pass

    if not session_verified:
        return False, f"replier session '{replier_sid}' is not recorded in authoritative session store"

    # 5. Addressing check: reply must be addressed to the bridge service tag
    if service_binding:
        msg_to = reply_msg.get("to")
        if isinstance(msg_to, dict):
            target_tag = msg_to.get("tag")
            if target_tag and target_tag != service_binding.tag:
                return False, f"reply addressed to '{target_tag}' instead of bridge '{service_binding.tag}'"

    return True, ""


def read_authoritative_cursor_safe(cursor_path: str) -> dict[str, Any] | None:
    """Safely read recipient cursor file avoiding symlinks and bounds exceeding 1 MiB.

    Returns None on any corruption, non-regular file, or syntax error.
    """
    if not os.path.isfile(cursor_path) or os.path.islink(cursor_path):
        return None

    try:
        data = json.loads(private_fs.read_bytes(cursor_path, MAX_MAILBOX_STATE_BYTES, private=False))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def create_fake_aplexer_script(target_path: str) -> None:
    """Create OWN absolute executable fake aplexer enforcing Rust messaging contracts.

    Faithfully enforces:
    - `message send`: Emits genuine sender metadata if APLEXER_SESSION_ID is bound to an
      authoritative session; emits `{"external": true}` (anonymous) otherwise.
    - `message reply`: Enforces Rust `reply_target` (lines 218-248). Fails with:
      `original message <id> was sent anonymously (no sender tag)` if original is anonymous.
    - `message log`: Lists all mailbox envelopes.
    - `message ack`: Records exact UUIDs into recipient cursor exceptions.
    - `status`: Returns idle state.
    - `capture`: Returns screen with prompt symbol.
    """
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    script_content = '''#!/usr/bin/env python3
import sys
import os
import json
import uuid
import time
import hashlib

def canonicalize(p):
    return os.path.realpath(os.path.abspath(p))

def ws_key(p):
    return hashlib.sha256(canonicalize(p).encode("utf-8")).hexdigest()[:32]

def find_session_record(sid, state_dir, runtime_dir):
    for root in [runtime_dir, state_dir]:
        if root:
            p = os.path.join(root, "sessions", str(sid), "session.json")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
    return None

def find_all_records(state_dir, runtime_dir):
    records = []
    seen = set()
    for root in [runtime_dir, state_dir]:
        if root and os.path.isdir(os.path.join(root, "sessions")):
            sdir = os.path.join(root, "sessions")
            for entry in os.listdir(sdir):
                rf = os.path.join(sdir, entry, "session.json")
                if os.path.isfile(rf) and entry not in seen:
                    try:
                        with open(rf, "r", encoding="utf-8") as f:
                            r = json.load(f)
                        if isinstance(r, dict):
                            seen.add(entry)
                            records.append(r)
                    except Exception:
                        pass
    return records

def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(1)

    state_dir = os.environ.get("APLEXER_STATE_DIR")
    runtime_dir = os.environ.get("APLEXER_RUNTIME_DIR")
    ambient_sid = os.environ.get("APLEXER_SESSION_ID")

    # Parse common flags: --workspace, --json
    ws_arg = None
    json_out = "--json" in args

    for i, a in enumerate(args):
        if a == "--workspace" and i + 1 < len(args):
            ws_arg = args[i + 1]

    workspace = canonicalize(ws_arg or os.environ.get("APLEXER_WORKSPACE") or os.getcwd())
    key = ws_key(workspace)

    cmd = args[0]
    if cmd == "whoami":
        if not ambient_sid:
            sys.exit(1)
        rec = find_session_record(ambient_sid, state_dir, runtime_dir)
        if not rec:
            sys.exit(1)
        if json_out:
            print(json.dumps(rec, indent=2))
        else:
            print(f"id: {rec.get('id')}")
        sys.exit(0)

    elif cmd == "status":
        if json_out:
            print(json.dumps({"state": "idle", "schema_version": 1}))
        else:
            print("idle")
        sys.exit(0)

    elif cmd == "capture":
        print("workspace: " + workspace)
        print("user@host:~$ ❯ ")
        sys.exit(0)

    elif cmd == "message":
        if len(args) < 2:
            sys.exit(1)
        sub = args[1]

        msgs_dir = os.path.join(state_dir, "messages", key, "msgs") if state_dir else None
        cursors_dir = os.path.join(state_dir, "messages", key, "cursors") if state_dir else None

        if sub == "send":
            # Extract arguments
            to_tag = None
            kind = "note"
            queue = "--queue" in args
            body = args[-1] if not args[-1].startswith("-") else ""

            for i, a in enumerate(args):
                if a == "--to" and i + 1 < len(args):
                    to_tag = args[i + 1]
                elif a == "--kind" and i + 1 < len(args):
                    kind = args[i + 1]

            if not to_tag:
                sys.stderr.write("Error: --to required\\n")
                sys.exit(1)

            # Resolve sender provenance
            sender_rec = find_session_record(ambient_sid, state_dir, runtime_dir) if ambient_sid else None
            if sender_rec and canonicalize(sender_rec.get("workspace", "")) == workspace:
                from_obj = {
                    "session_id": str(sender_rec["id"]),
                    "workspace": workspace,
                    "tag": str(sender_rec.get("tag")),
                    "engine": str(sender_rec.get("engine", "test")),
                    "external": False
                }
            else:
                from_obj = {"external": True}

            # Resolve recipient
            records = find_all_records(state_dir, runtime_dir)
            recip_rec = next((r for r in records if r.get("tag") == to_tag and canonicalize(r.get("workspace", "")) == workspace), None)
            if recip_rec:
                to_obj = {"tag": to_tag, "session_id": str(recip_rec["id"])}
            elif queue:
                to_obj = {"tag": to_tag}
            else:
                sys.stderr.write(f"Error: no session tagged {to_tag} exists\\n")
                sys.exit(1)

            msg_id = str(uuid.uuid4())
            envelope = {
                "schema_version": 1,
                "id": msg_id,
                "workspace": workspace,
                "created_at": int(time.time()),
                "from": from_obj,
                "to": to_obj,
                "kind": kind,
                "reply_to": None,
                "body": body,
                "delivery": "inbox"
            }

            if msgs_dir:
                os.makedirs(msgs_dir, exist_ok=True)
                with open(os.path.join(msgs_dir, f"{msg_id}.json"), "w", encoding="utf-8") as f:
                    json.dump(envelope, f, indent=2)

            if json_out:
                print(json.dumps(envelope))
            else:
                print(msg_id)
            sys.exit(0)

        elif sub == "reply":
            orig_id = args[2]
            body = args[-1] if not args[-1].startswith("-") else ""

            if not msgs_dir:
                sys.stderr.write("Error: no state_dir for messages\\n")
                sys.exit(1)

            orig_file = os.path.join(msgs_dir, f"{orig_id}.json")
            if not os.path.isfile(orig_file):
                sys.stderr.write(f"Error: no such message {orig_id}\\n")
                sys.exit(1)

            with open(orig_file, "r", encoding="utf-8") as f:
                orig = json.load(f)

            # RUST REPLY_TARGET CONTRACT (message_routing.rs:218-248)
            orig_from = orig.get("from", {})
            orig_tag = orig_from.get("tag")
            if not orig_tag:
                # Actual Rust error message verbatim:
                sys.stderr.write(f"Error: original message {orig_id} was sent anonymously (no sender tag)\\n")
                sys.exit(1)

            # Replier session resolution
            replier_rec = find_session_record(ambient_sid, state_dir, runtime_dir) if ambient_sid else None
            if not replier_rec:
                sys.stderr.write("Error: reply requires calling aplexer session\\n")
                sys.exit(1)

            from_obj = {
                "session_id": str(replier_rec["id"]),
                "workspace": workspace,
                "tag": str(replier_rec.get("tag")),
                "engine": str(replier_rec.get("engine", "test")),
                "external": False
            }

            to_obj = {
                "tag": orig_tag,
                "session_id": orig_from.get("session_id")
            }

            reply_id = str(uuid.uuid4())
            reply_envelope = {
                "schema_version": 1,
                "id": reply_id,
                "workspace": workspace,
                "created_at": int(time.time()),
                "from": from_obj,
                "to": to_obj,
                "kind": "reply",
                "reply_to": orig_id,
                "body": body,
                "delivery": "inbox"
            }

            with open(os.path.join(msgs_dir, f"{reply_id}.json"), "w", encoding="utf-8") as f:
                json.dump(reply_envelope, f, indent=2)

            if json_out:
                print(json.dumps(reply_envelope))
            else:
                print(reply_id)
            sys.exit(0)

        elif sub == "log":
            envelopes = []
            if msgs_dir and os.path.isdir(msgs_dir):
                for fname in sorted(os.listdir(msgs_dir)):
                    if fname.endswith(".json"):
                        try:
                            with open(os.path.join(msgs_dir, fname), "r", encoding="utf-8") as f:
                                envelopes.append(json.load(f))
                        except Exception:
                            pass
            envelopes.sort(key=lambda m: (m.get("created_at", 0), m.get("id", "")))
            print(json.dumps(envelopes))
            sys.exit(0)

        elif sub == "ack":
            ack_ids = [a for a in args[2:] if not a.startswith("-")]
            if not ambient_sid or not cursors_dir:
                sys.exit(1)

            os.makedirs(cursors_dir, exist_ok=True)
            cpath = os.path.join(cursors_dir, f"{ambient_sid}.json")
            cdata = {"acked_through": None, "exceptions": []}
            if os.path.isfile(cpath):
                try:
                    with open(cpath, "r", encoding="utf-8") as f:
                        cdata = json.load(f)
                except Exception:
                    pass

            exc_set = set(cdata.get("exceptions", []))
            for aid in ack_ids:
                exc_set.add(aid)
            cdata["exceptions"] = sorted(list(exc_set))

            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(cdata, f, indent=2)
            sys.exit(0)

        elif sub == "deliver":
            deliv_id = args[2] if len(args) > 2 else ""
            log_path = os.environ.get("APLEXER_FAKE_DELIVER_LOG")
            behavior = os.environ.get("APLEXER_FAKE_DELIVER_BEHAVIOR")
            if log_path:
                try:
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"deliver:{deliv_id}\\n")
                except Exception:
                    pass
            if behavior == "timeout":
                time.sleep(35)
                sys.exit(0)
            elif behavior == "exit_1":
                sys.stderr.write("delivery failed (exit 1)\\n")
                sys.exit(1)
            elif behavior == "effect_then_crash":
                sys.stderr.write("delivery crashed after effect\\n")
                sys.exit(1)
            sys.exit(0)

    sys.exit(0)

if __name__ == "__main__":
    main()
'''
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(script_content)
    os.chmod(target_path, 0o755)
