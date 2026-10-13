from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host.bridge import (
    BridgeCapabilityGap,
    MailboxBridge,
    canonicalize_workspace,
    get_authoritative_runtime_dir,
    get_authoritative_state_dir,
    get_workspace_key,
)


def get_test_tmp_dir() -> str:
    """Ensure temporary directories live under .local/tmp, never /tmp."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp"))
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(dir=root)


def create_authoritative_state(
    state_dir: str,
    canonical_ws: str,
    recipient_tag: str,
    recipient_session_id: str,
    exceptions: list[str] | None = None,
    acked_through: str | None = None,
    workspace_json_override: dict | None = None,
    runtime_dir: str | None = None,
    service_tag: str = "hub-bridge",
    service_session_id: str = "00000000-0000-0000-0000-000000000001",
) -> tuple[str, str]:
    """Helper to set up authentic aplexer state layout for tests."""
    target_sessions_root = runtime_dir if runtime_dir else state_dir

    # 1. Recipient session record
    sdir = os.path.join(target_sessions_root, "sessions", recipient_session_id)
    os.makedirs(sdir, exist_ok=True)
    session_rec = {
        "schema_version": 1,
        "id": recipient_session_id,
        "workspace": canonical_ws,
        "tag": recipient_tag,
        "engine": "test-engine",
        "created_at_ms": 1700000000000,
        "updated_at_ms": 1700000000000,
        "command": ["bash"],
        "cwd": canonical_ws
    }
    with open(os.path.join(sdir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(session_rec, f)

    # 2. Bridge service session record
    serv_dir = os.path.join(target_sessions_root, "sessions", service_session_id)
    os.makedirs(serv_dir, exist_ok=True)
    serv_rec = {
        "schema_version": 1,
        "id": service_session_id,
        "workspace": canonical_ws,
        "tag": service_tag,
        "engine": "hub-bridge",
        "created_at_ms": 1700000000000 - 1000,
        "command": ["hub-bridge-service"],
        "cwd": canonical_ws
    }
    with open(os.path.join(serv_dir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(serv_rec, f)

    # 3. Mailbox in state_dir/messages/<ws_key>
    ws_key = get_workspace_key(canonical_ws)
    mailbox_dir = os.path.join(state_dir, "messages", ws_key)
    cursors_dir = os.path.join(mailbox_dir, "cursors")
    os.makedirs(cursors_dir, exist_ok=True)

    # workspace.json
    ws_meta = workspace_json_override if workspace_json_override is not None else {"workspace": canonical_ws}
    with open(os.path.join(mailbox_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump(ws_meta, f)

    # 4. Recipient cursor in state_dir/messages/<ws_key>/cursors/<recipient_session_id>.json
    cursor_path = os.path.join(cursors_dir, f"{recipient_session_id}.json")
    cursor_data = {
        "acked_through": acked_through,
        "exceptions": exceptions or []
    }
    with open(cursor_path, "w", encoding="utf-8") as f:
        json.dump(cursor_data, f)

    return mailbox_dir, cursor_path


class TestBridge(unittest.TestCase):

    def test_state_dir_precedence(self):
        """Test authoritative state dir precedence: explicit > APLEXER_STATE_DIR > XDG_STATE_HOME > default."""
        # 1. Explicit override
        s1 = get_authoritative_state_dir(explicit_state_dir="/explicit/state")
        self.assertEqual(s1, "/explicit/state")

        # 2. APLEXER_STATE_DIR environment override
        with patch.dict(os.environ, {"APLEXER_STATE_DIR": "/env/aplexer/state", "XDG_STATE_HOME": "/xdg/state"}):
            s2 = get_authoritative_state_dir(None)
            self.assertEqual(s2, "/env/aplexer/state")

        # 3. XDG_STATE_HOME environment override
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/xdg/state"}, clear=True):
            s3 = get_authoritative_state_dir(None)
            self.assertEqual(s3, "/xdg/state/aplexer")

        # 4. Default home directory
        with patch.dict(os.environ, {"HOME": "/home/testuser"}, clear=True):
            s4 = get_authoritative_state_dir(None)
            self.assertEqual(s4, "/home/testuser/.local/state/aplexer")

    def test_wrong_consumer_cursor_denied(self):
        """Cursor file belonging to another consumer (wrong UUID) must NOT cause ACK."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)

        rec_uuid = str(uuid.uuid4())
        wrong_consumer_uuid = str(uuid.uuid4())
        native_id = str(uuid.uuid4())

        # Create authoritative state with recipient session
        mailbox_dir, _ = create_authoritative_state(
            state_dir, canonical_ws, "agent-1", rec_uuid, exceptions=[]
        )

        # Write cursor for the wrong consumer with the native_id in exceptions
        wrong_cursor = os.path.join(mailbox_dir, "cursors", f"{wrong_consumer_uuid}.json")
        with open(wrong_cursor, "w", encoding="utf-8") as f:
            json.dump({"exceptions": [native_id], "acked_through": None}, f)

        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-proof": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {
                "from_actor": "user",
                "to_actor": "agent:agent-1",
                "hub_id": "hub-proof"
            }
        }
        bridge._save_receipts()

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="failed")):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_not_called()
            self.assertNotIn("hub-proof", bridge.receipts.get("acked_hub_messages", []))

    def test_forged_or_mismatched_workspace_file_denied(self):
        """Mailbox workspace.json pointing to wrong workspace or missing must be rejected."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)

        rec_uuid = str(uuid.uuid4())
        native_id = str(uuid.uuid4())

        # 1. Mismatched workspace in workspace.json
        create_authoritative_state(
            state_dir, canonical_ws, "agent-1", rec_uuid,
            exceptions=[native_id],
            workspace_json_override={"workspace": "/different/workspace"}
        )

        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-1": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {"from_actor": "user", "to_actor": "agent-1", "hub_id": "hub-1"}
        }

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="[]")):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_not_called()

        # 2. Corrupt workspace.json
        ws_key = get_workspace_key(canonical_ws)
        meta_file = os.path.join(state_dir, "messages", ws_key, "workspace.json")
        with open(meta_file, "w", encoding="utf-8") as f:
            f.write("NOT-JSON-CONTENT{")

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="[]")):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_not_called()

    def test_workspace_local_cursor_spoof_denied(self):
        """Files placed inside workspace .local/cursors must be ignored (untrusted)."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)

        spoof_dir = os.path.join(work, ".local", "cursors")
        os.makedirs(spoof_dir, exist_ok=True)
        native_id = str(uuid.uuid4())
        with open(os.path.join(spoof_dir, "fake.json"), "w", encoding="utf-8") as f:
            json.dump({"exceptions": [native_id]}, f)

        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-1": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {"from_actor": "user", "to_actor": "agent-1", "hub_id": "hub-1"}
        }

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="no sessions")):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_not_called()

    def test_authoritative_correct_recipient_ack_once_across_two_cycles(self):
        """Valid recipient cursor causes exactly one hub bus.ack across two cycles."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)

        rec_uuid = str(uuid.uuid4())
        native_id = str(uuid.uuid4())

        create_authoritative_state(
            state_dir, canonical_ws, "agent-1", rec_uuid, exceptions=[native_id]
        )

        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-1": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {"from_actor": "alice", "to_actor": "agent:agent-1", "hub_id": "hub-1"}
        }
        bridge._save_receipts()

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="[]")):
            # Cycle 1: positive ACK triggered
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_called_once_with("hub-1")
            self.assertIn("hub-1", bridge.receipts["acked_hub_messages"])

            # Cycle 2: idempotent, no redundant ACK
            bridge.sync_native_to_hub()
            self.assertEqual(mock_bus.ack.call_count, 1)

    def test_legacy_acked_through_boundaries(self):
        """Legacy acked_through UUID ordering applies ONLY to recipient cursor, not other consumers."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)

        rec_uuid = str(uuid.uuid4())
        id_lower = "00000000-0000-0000-0000-000000000001"
        id_middle = "00000000-0000-0000-0000-000000000005"
        id_higher = "00000000-0000-0000-0000-000000000010"

        # Case 1: recipient cursor with acked_through = id_middle. id_lower <= id_middle -> ACK!
        create_authoritative_state(
            state_dir, canonical_ws, "agent-1", rec_uuid,
            exceptions=[],
            acked_through=id_middle
        )

        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-lower": id_lower, "hub-higher": id_higher}
        bridge.receipts["hub_messages"] = {
            id_lower: {"from_actor": "alice", "to_actor": "agent-1", "hub_id": "hub-lower"},
            id_higher: {"from_actor": "alice", "to_actor": "agent-1", "hub_id": "hub-higher"}
        }

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="[]")):
            bridge.sync_native_to_hub()
            # hub-lower is acked (<= middle), hub-higher is NOT acked (> middle)
            mock_bus.ack.assert_called_once_with("hub-lower")
            self.assertIn("hub-lower", bridge.receipts["acked_hub_messages"])
            self.assertNotIn("hub-higher", bridge.receipts["acked_hub_messages"])

    def test_failed_empty_malformed_native_reads_no_ack(self):
        """Corrupt cursor JSON, empty cursor file, or failed reads must fail closed with zero ACK."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)

        rec_uuid = str(uuid.uuid4())
        native_id = str(uuid.uuid4())

        _, cursor_path = create_authoritative_state(
            state_dir, canonical_ws, "agent-1", rec_uuid, exceptions=[native_id]
        )

        # Corrupt cursor file
        with open(cursor_path, "w", encoding="utf-8") as f:
            f.write("{invalid-json-content")

        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-1": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {"from_actor": "alice", "to_actor": "agent-1", "hub_id": "hub-1"}
        }

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="[]")):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_not_called()

    def test_reverse_reply_bound_original_parties_once(self):
        """Reverse reply preserves exact original actors, thread_id, and is bridged once."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        bob_sid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "bob", bob_sid)
        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-orig-1": "native-msg-1"}
        bridge.receipts["hub_messages"] = {
            "native-msg-1": {
                "from_actor": "projA:alice",
                "to_actor": "agent:bob",
                "thread_id": "thread-42",
                "hub_id": "hub-orig-1",
                "recipient_session_id": bob_sid,
            }
        }
        bridge._save_receipts()

        real_reply_log = [
            {
                "schema_version": 1,
                "id": "native-reply-99",
                "workspace": canonical_ws,
                "created_at": 1787738302,
                "from": {"tag": "bob", "session_id": bob_sid},
                "to": {"tag": "hub-bridge"},
                "kind": "reply",
                "reply_to": "native-msg-1",
                "body": "finished the task",
                "delivery": "inbox"
            }
        ]

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(real_reply_log))):
            mock_bus.reply.return_value = {"id": "hub-rep-100"}

            # Cycle 1: bridges reverse reply with exact actors and thread_id
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_called_once_with(
                "hub-orig-1",
                "agent:bob",
                "projA:alice",
                "reply",
                "finished the task",
                idempotency_key="native-reply-99",
                thread_id="thread-42"
            )
            self.assertEqual(bridge.receipts["native_to_hub"]["native-reply-99"], "hub-rep-100")

            # Cycle 2: idempotent, not sent again
            bridge.sync_native_to_hub()
            self.assertEqual(mock_bus.reply.call_count, 1)

    def test_unmapped_ambiguous_reply_refused(self):
        """Replies with unknown reply_to or missing provenance actors are refused."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        bridge = MailboxBridge(work)
        bridge.receipts["hub_to_native"] = {"hub-1": "native-1"}
        bridge.receipts["hub_messages"] = {
            "native-1": {"from_actor": None, "to_actor": "agent-1"}
        }

        unmapped_log = [
            {"id": "rep-unmapped", "kind": "reply", "reply_to": "unknown-id", "body": "text"},
            {"id": "rep-missing-actors", "kind": "reply", "reply_to": "native-1", "body": "text"}
        ]

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(unmapped_log))):
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_not_called()

    def test_receipt_envelope_and_native_id_persisted_mode_600_before_deliver(self):
        """Receipt is saved mode 0600 with envelope info BEFORE deliver runs."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        # Mock send success, but status check fails to simulate refusal during sync_reply delivery
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=json.dumps({
                    "schema_version": 1,
                    "id": "native-persisted-1",
                    "workspace": canonical_ws,
                    "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
                    "to": {"tag": "agent-1", "session_id": rec_uuid},
                })), # send --queue
                MagicMock(returncode=1, stderr="status failed") # status 1
            ]

            hub_msg = {
                "id": "hub-msg-atomic",
                "to_actor": "agent:agent-1",
                "from_actor": "user-alice",
                "body": "sync work",
                "kind": "sync_reply",
                "thread_id": "thread-1"
            }

            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

            # Assert receipts file exists and has mode 0600
            self.assertTrue(os.path.isfile(bridge.receipts_file))
            file_mode = stat.S_IMODE(os.stat(bridge.receipts_file).st_mode)
            self.assertEqual(file_mode, 0o600)

            # Assert mapping was persisted BEFORE delivery refusal
            self.assertEqual(bridge.receipts["hub_to_native"]["hub-msg-atomic"], "native-persisted-1")
            saved_info = bridge.receipts["hub_messages"]["native-persisted-1"]
            self.assertEqual(saved_info["from_actor"], "user-alice")
            self.assertEqual(saved_info["to_actor"], "agent:agent-1")
            self.assertEqual(saved_info["thread_id"], "thread-1")

    def test_forward_queue_once_retry_existing_id(self):
        """Forwarding an already queued hub message returns existing ID without invoking send again."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({
                "schema_version": 1,
                "id": "native-first",
                "workspace": canonical_ws,
                "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
                "to": {"tag": "agent-1", "session_id": rec_uuid},
            }))

            hub_msg = {"id": "hub-dedup-1", "to_actor": "agent-1", "body": "hello", "kind": "note"}

            # Call 1: sends
            nid1 = bridge.forward_hub_to_native(hub_msg)
            self.assertEqual(nid1, "native-first")
            self.assertEqual(mock_run.call_count, 1)

            # Call 2: reuses existing ID
            nid2 = bridge.forward_hub_to_native(hub_msg)
            self.assertEqual(nid2, "native-first")
            self.assertEqual(mock_run.call_count, 1)

    def test_uncertain_outcome_no_resend(self):
        """Failed native send raises RuntimeError and does not save receipt."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fatal error")

            hub_msg = {"id": "hub-fail-1", "to_actor": "agent-1", "body": "hello", "kind": "note"}

            with self.assertRaises(RuntimeError):
                bridge.forward_hub_to_native(hub_msg)

            self.assertNotIn("hub-fail-1", bridge.receipts.get("hub_to_native", {}))

    def test_sync_reply_all_refusals_and_success(self):
        """Sync reply checks two status+capture observations then immediate final check."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        hub_msg = {"id": "sync-1", "to_actor": "agent-1", "body": "wake", "kind": "sync_reply"}
        valid_send_out = json.dumps({
            "schema_version": 1,
            "id": "nid-1",
            "workspace": canonical_ws,
            "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
            "to": {"tag": "agent-1", "session_id": rec_uuid}
        })
        valid_send_ok = json.dumps({
            "schema_version": 1,
            "id": "nid-ok",
            "workspace": canonical_ws,
            "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
            "to": {"tag": "agent-1", "session_id": rec_uuid}
        })

        # 1. Busy status refusal
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=valid_send_out),
                MagicMock(returncode=0, stdout='{"state":"busy"}')
            ]
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        # 2. Waiting/unknown status refusal
        bridge.receipts = {}
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=valid_send_out),
                MagicMock(returncode=0, stdout='{"state":"unknown"}')
            ]
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        # 3. Draft in composer refusal (text after prompt)
        bridge.receipts = {}
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=valid_send_out),
                MagicMock(returncode=0, stdout='{"state":"idle"}'),
                MagicMock(returncode=0, stdout='prompt ❯ typed half draft')
            ]
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        # 4. Interactive menu on screen refusal
        bridge.receipts = {}
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=valid_send_out),
                MagicMock(returncode=0, stdout='{"state":"idle"}'),
                MagicMock(returncode=0, stdout='? Select an option:\n[1] Yes\n[2] No ❯')
            ]
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        # 5. Valid fake idle + empty composer -> SUCCESS!
        bridge.receipts = {}
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=valid_send_ok),  # send
                MagicMock(returncode=0, stdout='{"state":"idle"}'),  # obs 1 status
                MagicMock(returncode=0, stdout='welcome\nprompt ❯'), # obs 1 capture
                MagicMock(returncode=0, stdout='{"state":"idle"}'),  # obs 2 status
                MagicMock(returncode=0, stdout='welcome\nprompt ❯'), # obs 2 capture
                MagicMock(returncode=0, stdout='{"state":"idle"}'),  # immediate final check status
                MagicMock(returncode=0, stdout='welcome\nprompt ❯'), # immediate final check capture
                MagicMock(returncode=0, stdout='{"id":"nid-ok"}')   # deliver
            ]
            delivered_id = bridge.forward_hub_to_native(hub_msg)
            self.assertEqual(delivered_id, "nid-ok")

    def test_no_identity_overrides_in_any_commands(self):
        """Verify the bridge never executes aplexer message inbox with --from or overrides identity."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        executed_commands = []

        def record_run(cmd, *args, **kwargs):
            executed_commands.append(cmd)
            if "send" in cmd:
                return MagicMock(returncode=0, stdout=json.dumps({
                    "schema_version": 1,
                    "id": "nid-1",
                    "workspace": canonical_ws,
                    "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
                    "to": {"tag": "agent-1", "session_id": rec_uuid}
                }))
            elif "log" in cmd:
                return MagicMock(returncode=0, stdout='[]')
            return MagicMock(returncode=0, stdout='[]')

        with patch("subprocess.run", side_effect=record_run), patch("agent_hub_host.bridge.bus"):
            bridge.forward_hub_to_native({"id": "h-1", "to_actor": "agent-1", "body": "msg", "kind": "note"})
            bridge.sync_native_to_hub()

        for cmd in executed_commands:
            self.assertNotIn("--from", cmd, f"Forbidden identity override --from found in: {cmd}")
            self.assertNotIn("inbox", cmd, f"Forbidden inbox override command found in: {cmd}")

    def test_fake_a_binary_execution_end_to_end(self):
        """Execute end-to-end round trip against an executable fake-a binary with genuine UUID state."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)

        rec_uuid = str(uuid.uuid4())
        native_msg_id = str(uuid.uuid4())
        native_reply_id = str(uuid.uuid4())

        # Set up authoritative state with registered session and positive cursor
        create_authoritative_state(
            state_dir, canonical_ws, "agent-1", rec_uuid, exceptions=[native_msg_id]
        )

        fake_a = os.path.join(td, "fake-a")
        with open(fake_a, "w", encoding="utf-8") as f:
            f.write(f'''#!/usr/bin/env python3
import sys, json

args = sys.argv[1:]
if not args:
    sys.exit(1)

cmd = args[0]
if cmd == "message":
    sub = args[1]
    if sub == "send":
        body = args[-1]
        print(json.dumps({{
            "schema_version": 1,
            "id": "{native_msg_id}",
            "workspace": "{canonical_ws}",
            "created_at": 1787738302,
            "from": {{"tag": "hub-bridge", "session_id": "00000000-0000-0000-0000-000000000001", "workspace": "{canonical_ws}"}},
            "to": {{"tag": "agent-1", "session_id": "{rec_uuid}"}},
            "kind": "note",
            "reply_to": None,
            "body": body,
            "delivery": "inbox"
        }}))
        sys.exit(0)
    elif sub == "log":
        print(json.dumps([{{
            "schema_version": 1,
            "id": "{native_reply_id}",
            "workspace": "{canonical_ws}",
            "created_at": 1787738310,
            "from": {{"tag": "agent-1", "session_id": "{rec_uuid}", "workspace": "{canonical_ws}"}},
            "to": {{"tag": "hub-bridge"}},
            "kind": "reply",
            "reply_to": "{native_msg_id}",
            "body": "processed by fake-a",
            "delivery": "inbox"
        }}]))
        sys.exit(0)
sys.exit(0)
''')
        os.chmod(fake_a, 0o755)

        bridge = MailboxBridge(work, aplexer_bin=fake_a, state_dir=state_dir)

        with patch("agent_hub_host.bridge.bus") as mock_bus:
            mock_bus.ack.return_value = {"status": "ok"}
            mock_bus.reply.return_value = {"id": "hub-reply-final"}

            # 1. Forward
            nid = bridge.forward_hub_to_native({
                "id": "hub-msg-100",
                "to_actor": "agent-1",
                "from_actor": "projA:user",
                "body": "do task",
                "kind": "note",
                "thread_id": "thread-100"
            })
            self.assertEqual(nid, native_msg_id)

            # 2. Sync native to hub
            bridge.sync_native_to_hub()

            # Positive ACK verified
            mock_bus.ack.assert_called_once_with("hub-msg-100")
            # Reverse reply verified
            mock_bus.reply.assert_called_once_with(
                "hub-msg-100",
                "agent-1",
                "projA:user",
                "reply",
                "processed by fake-a",
                idempotency_key=native_reply_id,
                thread_id="thread-100"
            )

    def test_runtime_dir_precedence(self):
        """Test authoritative runtime dir precedence: explicit > APLEXER_RUNTIME_DIR > XDG_RUNTIME_DIR > default."""
        # 1. Explicit override
        r1 = get_authoritative_runtime_dir(explicit_runtime_dir="/explicit/runtime")
        self.assertEqual(r1, "/explicit/runtime")

        # 2. APLEXER_RUNTIME_DIR environment override
        with patch.dict(os.environ, {"APLEXER_RUNTIME_DIR": "/env/aplexer/runtime", "XDG_RUNTIME_DIR": "/xdg/runtime"}):
            r2 = get_authoritative_runtime_dir(None)
            self.assertEqual(r2, "/env/aplexer/runtime")

        # 3. XDG_RUNTIME_DIR environment override
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/xdg/runtime"}, clear=True):
            r3 = get_authoritative_runtime_dir(None)
            self.assertEqual(r3, "/xdg/runtime/aplexer")

        # 4. Default /tmp/aplexer-<uid>
        with patch.dict(os.environ, {}, clear=True):
            r4 = get_authoritative_runtime_dir(None)
            self.assertTrue(r4.startswith("/tmp/aplexer-"))

    def test_distinct_runtime_and_state_directories_no_mirror(self):
        """Sessions live only in runtime_dir and messages only in state_dir with NO state session mirror."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        runtime_dir = os.path.join(td, "native-runtime")
        state_dir = os.path.join(td, "native-state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)

        rec_uuid = str(uuid.uuid4())
        native_id = str(uuid.uuid4())

        # Create state with session in runtime_dir only, messages in state_dir
        mailbox_dir, cursor_path = create_authoritative_state(
            state_dir, canonical_ws, "worker-agent", rec_uuid,
            exceptions=[native_id],
            runtime_dir=runtime_dir
        )

        # Assert no sessions mirror exists in state_dir
        self.assertFalse(os.path.exists(os.path.join(state_dir, "sessions")))
        # Assert session exists in runtime_dir
        self.assertTrue(os.path.isfile(os.path.join(runtime_dir, "sessions", rec_uuid, "session.json")))

        bridge = MailboxBridge(work, state_dir=state_dir, runtime_dir=runtime_dir)
        bridge.receipts["hub_to_native"] = {"hub-msg-42": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {
                "from_actor": "admin",
                "to_actor": "worker-agent",
                "hub_id": "hub-msg-42"
            }
        }
        bridge._save_receipts()

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="[]")):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_called_once_with("hub-msg-42")
            self.assertIn("hub-msg-42", bridge.receipts["acked_hub_messages"])

    def test_native_reply_external_sender_rejected(self):
        """Native replies from external senders (no session identity) must be rejected."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        bridge = MailboxBridge(work)
        bridge.receipts["hub_to_native"] = {"hub-orig": "native-orig"}
        bridge.receipts["hub_messages"] = {
            "native-orig": {
                "from_actor": "projA:lead",
                "to_actor": "worker",
                "hub_id": "hub-orig",
                "recipient_session_id": "sess-real-123"
            }
        }
        bridge._save_receipts()

        external_reply_log = [{
            "id": "native-rep-ext",
            "from": {"external": True},
            "to": {"tag": "lead"},
            "reply_to": "native-orig",
            "body": "external poke",
            "kind": "reply"
        }]

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(external_reply_log))):
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_not_called()
            self.assertNotIn("native-rep-ext", bridge.receipts.get("native_to_hub", {}))

    def test_native_reply_mismatched_session_rejected(self):
        """Native replies with mismatched session ID provenance must be rejected."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        bridge = MailboxBridge(work)
        bridge.receipts["hub_to_native"] = {"hub-orig": "native-orig"}
        bridge.receipts["hub_messages"] = {
            "native-orig": {
                "from_actor": "projA:lead",
                "to_actor": "worker",
                "hub_id": "hub-orig",
                "recipient_session_id": "sess-real-123"
            }
        }
        bridge._save_receipts()

        mismatched_reply_log = [{
            "id": "native-rep-impostor",
            "from": {"session_id": "sess-impostor-999", "tag": "worker"},
            "to": {"tag": "lead"},
            "reply_to": "native-orig",
            "body": "impostor reply",
            "kind": "reply"
        }]

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(mismatched_reply_log))):
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_not_called()
            self.assertNotIn("native-rep-impostor", bridge.receipts.get("native_to_hub", {}))

    def test_native_reply_matching_provenance_success(self):
        """Native replies with matching session ID provenance are bridged with exact actors."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        sess_id = str(uuid.uuid4())

        sdir = os.path.join(state_dir, "sessions", sess_id)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "session.json"), "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": 1,
                "id": sess_id,
                "workspace": canonical_ws,
                "tag": "worker",
                "engine": "test",
            }, f)

        bridge = MailboxBridge(work, state_dir=state_dir)
        bridge.receipts["hub_to_native"] = {"hub-orig": "native-orig"}
        bridge.receipts["hub_messages"] = {
            "native-orig": {
                "from_actor": "projA:lead",
                "to_actor": "worker",
                "hub_id": "hub-orig",
                "thread_id": "thread-verified-7",
                "recipient_session_id": sess_id
            }
        }
        bridge._save_receipts()

        valid_reply_log = [{
            "id": "native-rep-valid",
            "workspace": canonical_ws,
            "from": {"session_id": sess_id, "tag": "worker"},
            "to": {"tag": "lead"},
            "reply_to": "native-orig",
            "body": "authentic reply",
            "kind": "reply"
        }]

        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(valid_reply_log))):
            mock_bus.reply.return_value = {"id": "hub-rep-456"}
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_called_once_with(
                "hub-orig",
                "worker",
                "projA:lead",
                "reply",
                "authentic reply",
                idempotency_key="native-rep-valid",
                thread_id="thread-verified-7"
            )
            self.assertEqual(bridge.receipts["native_to_hub"]["native-rep-valid"], "hub-rep-456")


if __name__ == "__main__":
    unittest.main()
