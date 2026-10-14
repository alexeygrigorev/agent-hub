from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host.bridge import (
    BridgeCapabilityGap,
    MailboxBridge,
    canonicalize_workspace,
    composer_is_empty,
    get_authoritative_runtime_dir,
    get_authoritative_state_dir,
    get_workspace_key,
    native_tag_for_address,
    normalize_native_kind,
    poll_once,
    registered_bridge_targets,
)
from agent_hub_host.native_bridge import validate_native_reply_provenance


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
        """Failed native send records ambiguous send, raises BridgeCapabilityGap, and retry refuses resend."""
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

            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

            self.assertNotIn("hub-fail-1", bridge.receipts.get("hub_to_native", {}))
            self.assertIn("hub-fail-1", bridge.receipts.get("ambiguous_sends", {}))
            amb = bridge.receipts["ambiguous_sends"]["hub-fail-1"]
            self.assertEqual(amb.get("status"), "uncertain")
            self.assertEqual(amb.get("returncode"), 1)

            # Retry attempt refuses to resend and makes zero additional subprocess calls
            mock_run.reset_mock()
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("prior ambiguous send", str(ctx.exception).lower())
            mock_run.assert_not_called()

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

    def test_b1_deliver_intent_recorded_before_effect(self):
        """Durable deliver intent is written to disk before message deliver execution."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        native_id = "native-intent-1"
        hub_msg = {"id": "hub-intent-1", "to_actor": "agent-1", "body": "hi", "kind": "sync_reply"}

        intent_checked = []

        def mock_subprocess(cmd, *args, **kwargs):
            if "send" in cmd:
                return MagicMock(returncode=0, stdout=json.dumps({
                    "schema_version": 1,
                    "id": native_id,
                    "workspace": canonical_ws,
                    "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
                    "to": {"tag": "agent-1", "session_id": rec_uuid},
                }))
            elif "status" in cmd:
                return MagicMock(returncode=0, stdout='{"state":"idle"}')
            elif "capture" in cmd:
                return MagicMock(returncode=0, stdout='prompt ❯')
            elif "deliver" in cmd:
                # Check disk receipts at moment of deliver invocation
                with open(bridge.receipts_file, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
                deliv_entry = disk_data.get("delivers", {}).get(native_id, {})
                intent_checked.append(deliv_entry.get("status"))
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=mock_subprocess):
            bridge.forward_hub_to_native(hub_msg)

        self.assertEqual(intent_checked, ["intent"])
        self.assertEqual(bridge.receipts["delivers"][native_id]["status"], "delivered")

    def test_b1_deliver_timeout_marks_uncertain_and_refuses_duplicate_pty_write_on_retry(self):
        """Delivery timeout marks state uncertain, records ambiguous send, and retry does zero additional writes."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        native_id = "native-timeout-1"
        hub_msg = {"id": "hub-timeout-1", "to_actor": "agent-1", "body": "hi", "kind": "sync_reply"}

        def mock_subprocess_timeout(cmd, *args, **kwargs):
            if "send" in cmd:
                return MagicMock(returncode=0, stdout=json.dumps({
                    "schema_version": 1,
                    "id": native_id,
                    "workspace": canonical_ws,
                    "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
                    "to": {"tag": "agent-1", "session_id": rec_uuid},
                }))
            elif "status" in cmd:
                return MagicMock(returncode=0, stdout='{"state":"idle"}')
            elif "capture" in cmd:
                return MagicMock(returncode=0, stdout='prompt ❯')
            elif "deliver" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=mock_subprocess_timeout):
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("timed out", str(ctx.exception).lower())

        self.assertEqual(bridge.receipts["delivers"][native_id]["status"], "timeout")
        self.assertIn("hub-timeout-1", bridge.receipts["ambiguous_sends"])

        # Retry forward must refuse immediately without running deliver subprocess again
        with patch("subprocess.run") as mock_run:
            with self.assertRaises(BridgeCapabilityGap) as ctx2:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("refusing redelivery", str(ctx2.exception).lower())
            mock_run.assert_not_called()

    def test_b1_deliver_effect_then_crash_refuses_additional_write_on_restart(self):
        """Deliver nonzero exit marks state uncertain, and restart refuses retry without duplicate write."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        native_id = "native-crash-1"
        hub_msg = {"id": "hub-crash-1", "to_actor": "agent-1", "body": "hi", "kind": "sync_reply"}

        def mock_subprocess_crash(cmd, *args, **kwargs):
            if "send" in cmd:
                return MagicMock(returncode=0, stdout=json.dumps({
                    "schema_version": 1,
                    "id": native_id,
                    "workspace": canonical_ws,
                    "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
                    "to": {"tag": "agent-1", "session_id": rec_uuid},
                }))
            elif "status" in cmd:
                return MagicMock(returncode=0, stdout='{"state":"idle"}')
            elif "capture" in cmd:
                return MagicMock(returncode=0, stdout='prompt ❯')
            elif "deliver" in cmd:
                return MagicMock(returncode=1, stderr="fatal error during PTY flush")
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=mock_subprocess_crash):
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        # Simulate restart with new MailboxBridge instance
        bridge2 = MailboxBridge(work, state_dir=state_dir)
        self.assertEqual(bridge2.receipts["delivers"][native_id]["status"], "uncertain")

        # Retry refuses and executes zero additional writes
        with patch("subprocess.run") as mock_run:
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge2.forward_hub_to_native(hub_msg)
            self.assertIn("refusing redelivery", str(ctx.exception).lower())
            mock_run.assert_not_called()

    def test_b1_native_popped_message_alone_does_not_prove_delivery(self):
        """Native popped message alone does NOT prove delivery; missing message without cursor ACK stays held."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        mailbox_dir, cursor_path = create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        bridge = MailboxBridge(work, state_dir=state_dir)
        native_id = "native-popped-1"
        hub_id = "hub-popped-1"
        bridge.receipts["hub_to_native"][hub_id] = native_id
        bridge.receipts["hub_messages"][native_id] = {
            "from_actor": "alice",
            "to_actor": "agent-1",
            "hub_id": hub_id,
            "recipient_session_id": rec_uuid,
            "kind": "sync_reply"
        }
        bridge.receipts["delivers"][native_id] = {
            "hub_id": hub_id,
            "status": "intent",
            "attempted_at": 1000
        }
        bridge._save_receipts()

        # Recipient cursor exceptions is empty (no positive ACK), and native message file is absent (popped)
        msg_file = os.path.join(mailbox_dir, "msgs", f"{native_id}.json")
        self.assertFalse(os.path.exists(msg_file))

        with patch("subprocess.run") as mock_run:
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native({
                    "id": hub_id,
                    "to_actor": "agent-1",
                    "from_actor": "alice",
                    "body": "msg",
                    "kind": "sync_reply"
                })
            self.assertIn("prior uncertain/intent delivery state", str(ctx.exception))
            mock_run.assert_not_called()

    def test_b1_reconcile_only_genuine_terminal_delivery_evidence(self):
        """Uncertain delivery reconciles when recipient cursor provides positive ACK; zero additional writes."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        mailbox_dir, cursor_path = create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        bridge = MailboxBridge(work, state_dir=state_dir)
        native_id = "native-reconcile-1"
        hub_id = "hub-reconcile-1"
        bridge.receipts["hub_to_native"][hub_id] = native_id
        bridge.receipts["hub_messages"][native_id] = {
            "from_actor": "alice",
            "to_actor": "agent-1",
            "hub_id": hub_id,
            "recipient_session_id": rec_uuid,
            "kind": "sync_reply"
        }
        bridge.receipts["delivers"][native_id] = {
            "hub_id": hub_id,
            "status": "uncertain",
            "attempted_at": 1000
        }
        bridge._save_receipts()

        # Update recipient cursor with positive ACK for native_id
        with open(cursor_path, "w", encoding="utf-8") as f:
            json.dump({"exceptions": [native_id], "acked_through": None}, f)

        # Retry forward reconciles to delivered without running deliver subprocess again
        with patch("subprocess.run") as mock_run:
            res_nid = bridge.forward_hub_to_native({
                "id": hub_id,
                "to_actor": "agent-1",
                "from_actor": "alice",
                "body": "msg",
                "kind": "sync_reply"
            })
            self.assertEqual(res_nid, native_id)
            self.assertEqual(bridge.receipts["delivers"][native_id]["status"], "delivered")
            mock_run.assert_not_called()

    def test_b1_successful_replay_does_not_redeliver(self):
        """Replay of already delivered sync_reply does not run status, capture, or deliver again."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir)

        native_id = "native-replay-ok"
        hub_id = "hub-replay-ok"
        bridge.receipts["hub_to_native"][hub_id] = native_id
        bridge.receipts["hub_messages"][native_id] = {
            "from_actor": "alice",
            "to_actor": "agent-1",
            "hub_id": hub_id,
            "recipient_session_id": rec_uuid,
            "kind": "sync_reply"
        }
        bridge.receipts["delivers"][native_id] = {
            "hub_id": hub_id,
            "status": "delivered",
            "delivered_at": 1000
        }
        bridge._save_receipts()

        with patch("subprocess.run") as mock_run:
            res_id = bridge.forward_hub_to_native({
                "id": hub_id,
                "to_actor": "agent-1",
                "from_actor": "alice",
                "body": "msg",
                "kind": "sync_reply"
            })
            self.assertEqual(res_id, native_id)
            mock_run.assert_not_called()

    def test_b1_receipt_storage_retention_bounded(self):
        """Acked receipt retention is pruned to MAX_RECEIPTS_ENTRIES."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        bridge = MailboxBridge(work)

        # Populate with 1100 acked entries
        for i in range(1100):
            hid = f"hub-old-{i}"
            nid = f"native-old-{i}"
            bridge.receipts["hub_to_native"][hid] = nid
            bridge.receipts["hub_messages"][nid] = {"hub_id": hid}
            bridge.receipts["acked_hub_messages"].append(hid)
            bridge.receipts["delivers"][nid] = {"hub_id": hid, "status": "delivered"}

        bridge._save_receipts()

        loaded = bridge._load_receipts()
        self.assertLessEqual(len(loaded["acked_hub_messages"]), 1000)
        self.assertLessEqual(len(loaded["hub_to_native"]), 1000)

    def test_b2_absent_binding_with_external_false_refused_and_recorded_ambiguous(self):
        """Bridge without own qualified session binding refuses envelope with external:false into ambiguous_sends."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        canonical_ws = canonicalize_workspace(work)
        # Empty state dir => qualify_native_service_binding returns None
        bridge = MailboxBridge(work)
        self.assertIsNone(bridge.service_binding)

        hub_msg = {"id": "hub-b2-1", "to_actor": "agent-1", "body": "msg", "kind": "note"}

        # Simulate CLI emitting from with external:false but tag 'alex' (not a bridge service binding)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps({
            "schema_version": 1,
            "id": "native-b2-1",
            "workspace": canonical_ws,
            "from": {"tag": "alex", "session_id": str(uuid.uuid4()), "external": False},
            "to": {"tag": "agent-1"},
            "body": "msg"
        }))):
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("unqualified", str(ctx.exception).lower())

        # Assert no positive receipt in hub_to_native
        self.assertNotIn("hub-b2-1", bridge.receipts.get("hub_to_native", {}))
        # Assert ambiguous send evidence recorded
        self.assertIn("hub-b2-1", bridge.receipts.get("ambiguous_sends", {}))

    def test_b2_wrong_session_in_send_envelope_refused(self):
        """Envelope sender session mismatching bridge service binding is refused and recorded ambiguous."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        serv_uuid = str(uuid.uuid4())
        create_authoritative_state(
            state_dir, canonical_ws, "agent-1", rec_uuid,
            service_session_id=serv_uuid, service_tag="hub-bridge"
        )
        bridge = MailboxBridge(work, state_dir=state_dir, service_session_id=serv_uuid, service_tag="hub-bridge")
        self.assertIsNotNone(bridge.service_binding)

        wrong_sid = str(uuid.uuid4())
        hub_msg = {"id": "hub-b2-wrong-sid", "to_actor": "agent-1", "body": "msg", "kind": "note"}

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps({
            "schema_version": 1,
            "id": "native-b2-wrong",
            "workspace": canonical_ws,
            "from": {"tag": "hub-bridge", "session_id": wrong_sid, "external": False},
            "to": {"tag": "agent-1", "session_id": rec_uuid},
            "body": "msg"
        }))):
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        self.assertNotIn("hub-b2-wrong-sid", bridge.receipts.get("hub_to_native", {}))
        self.assertIn("hub-b2-wrong-sid", bridge.receipts.get("ambiguous_sends", {}))

    def test_b2_symlinked_recipient_record_rejected(self):
        """Symlinked recipient session.json must NOT be accepted during recipient resolution."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        real_sess_dir = os.path.join(td, "real-sess")
        os.makedirs(real_sess_dir, exist_ok=True)
        real_json = os.path.join(real_sess_dir, "session.json")
        rec_id = str(uuid.uuid4())
        with open(real_json, "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": 1,
                "id": rec_id,
                "workspace": canonical_ws,
                "tag": "sym-agent"
            }, f)

        # Place symlink inside state_dir/sessions/<rec_id>/session.json pointing to real_json
        target_sdir = os.path.join(state_dir, "sessions", rec_id)
        os.makedirs(target_sdir, exist_ok=True)
        symlink_json = os.path.join(target_sdir, "session.json")
        os.symlink(real_json, symlink_json)

        bridge = MailboxBridge(work, state_dir=state_dir)
        resolved = bridge._resolve_recipient_session_id("agent:sym-agent", {})
        self.assertIsNone(resolved)

    def test_tightened_reverse_provenance_empty_sessions_dirs_unknown_deny(self):
        """Empty sessions_dirs returns UNKNOWN deny rather than failing open."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        canonical_ws = canonicalize_workspace(work)

        reply_msg = {
            "id": "reply-1",
            "workspace": canonical_ws,
            "from": {"tag": "worker", "session_id": str(uuid.uuid4())},
            "to": {"tag": "hub-bridge"},
            "reply_to": "orig-1"
        }
        original_info = {"to_actor": "agent:worker", "from_actor": "lead"}

        valid, reason = validate_native_reply_provenance(
            reply_msg, original_info, canonical_ws, None, None, None
        )
        self.assertFalse(valid)
        self.assertIn("UNKNOWN", reason)

    def test_bf1_send_persists_then_timeout_zero_second_send_on_restart(self):
        """Send persists then TimeoutExpired: restart/retry executes zero second send against fake binary."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        log_file = os.path.join(td, "send_sideeffects.log")
        fake_bin = os.path.join(td, "fake-timeout-bin")
        with open(fake_bin, "w", encoding="utf-8") as f:
            f.write(f'''#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if "send" in args:
    with open("{log_file}", "a", encoding="utf-8") as f:
        f.write("send_envelope_persisted\\n")
    sys.exit(0)
sys.exit(0)
''')
        os.chmod(fake_bin, 0o755)

        bridge = MailboxBridge(work, aplexer_bin=fake_bin, state_dir=state_dir)
        hub_msg = {"id": "hub-bf1-timeout", "to_actor": "agent-1", "body": "hello", "kind": "note"}

        orig_run = subprocess.run
        def mock_timeout_run(cmd, *args, **kwargs):
            if "send" in cmd:
                # Execute fake script so envelope persistence sideeffect occurs
                orig_run(cmd, *args, **kwargs)
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=180)
            return orig_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=mock_timeout_run):
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("timed out", str(ctx.exception).lower())

        # Verify sideeffect was recorded exactly once and ambiguous_sends is persisted
        with open(log_file, "r", encoding="utf-8") as f:
            lines_1 = f.readlines()
        self.assertEqual(len(lines_1), 1)
        self.assertIn("hub-bf1-timeout", bridge.receipts["ambiguous_sends"])
        self.assertEqual(bridge.receipts["ambiguous_sends"]["hub-bf1-timeout"]["status"], "timeout")

        # Restart with new MailboxBridge instance from disk
        bridge_restarted = MailboxBridge(work, aplexer_bin=fake_bin, state_dir=state_dir)

        # Retry forward must refuse immediately without executing fake binary again
        with patch("subprocess.run", side_effect=mock_timeout_run):
            with self.assertRaises(BridgeCapabilityGap) as ctx2:
                bridge_restarted.forward_hub_to_native(hub_msg)
            self.assertIn("prior ambiguous send", str(ctx2.exception).lower())

        with open(log_file, "r", encoding="utf-8") as f:
            lines_2 = f.readlines()
        self.assertEqual(len(lines_2), 1, "Expected zero second send after restart")

    def test_bf1_send_persists_then_nonzero_zero_second_send_on_restart(self):
        """Send persists then nonzero exit: restart/retry executes zero second send against fake binary."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        log_file = os.path.join(td, "send_nonzero_sideeffects.log")
        fake_bin = os.path.join(td, "fake-nonzero-bin")
        with open(fake_bin, "w", encoding="utf-8") as f:
            f.write(f'''#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if "send" in args:
    with open("{log_file}", "a", encoding="utf-8") as f:
        f.write("send_envelope_persisted_then_crashed\\n")
    sys.stderr.write("failed during response delivery\\n")
    sys.exit(1)
sys.exit(0)
''')
        os.chmod(fake_bin, 0o755)

        bridge = MailboxBridge(work, aplexer_bin=fake_bin, state_dir=state_dir)
        hub_msg = {"id": "hub-bf1-nonzero", "to_actor": "agent-1", "body": "hello", "kind": "note"}

        with self.assertRaises(BridgeCapabilityGap) as ctx:
            bridge.forward_hub_to_native(hub_msg)
        self.assertIn("native send failed", str(ctx.exception).lower())

        with open(log_file, "r", encoding="utf-8") as f:
            lines_1 = f.readlines()
        self.assertEqual(len(lines_1), 1)
        self.assertIn("hub-bf1-nonzero", bridge.receipts["ambiguous_sends"])
        self.assertEqual(bridge.receipts["ambiguous_sends"]["hub-bf1-nonzero"]["status"], "uncertain")
        self.assertEqual(bridge.receipts["ambiguous_sends"]["hub-bf1-nonzero"]["returncode"], 1)

        # Restart with new MailboxBridge instance from disk
        bridge_restarted = MailboxBridge(work, aplexer_bin=fake_bin, state_dir=state_dir)

        # Retry forward must refuse immediately without executing fake binary again
        with self.assertRaises(BridgeCapabilityGap) as ctx2:
            bridge_restarted.forward_hub_to_native(hub_msg)
        self.assertIn("prior ambiguous send", str(ctx2.exception).lower())

        with open(log_file, "r", encoding="utf-8") as f:
            lines_2 = f.readlines()
        self.assertEqual(len(lines_2), 1, "Expected zero second send after restart")

    def test_bf1_send_intent_persisted_before_execution_missing_outcome_held_unknown(self):
        """Missing send outcome (crash after intent recorded) stays held as unknown; zero send on retry."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        bridge = MailboxBridge(work, state_dir=state_dir)
        hub_msg = {"id": "hub-intent-missing", "to_actor": "agent-1", "body": "hello", "kind": "note"}

        # Simulate crash: send intent recorded on disk before process completed
        bridge.receipts["ambiguous_sends"]["hub-intent-missing"] = {
            "native_id": None,
            "status": "intent",
            "reason": "Send intent recorded before execution",
            "created_at": 1700000000,
        }
        bridge._save_receipts()

        # Restart bridge
        bridge_restarted = MailboxBridge(work, state_dir=state_dir)

        with patch("subprocess.run") as mock_run:
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge_restarted.forward_hub_to_native(hub_msg)
            self.assertIn("prior ambiguous send", str(ctx.exception).lower())
            mock_run.assert_not_called()

    def test_bf1_before_execution_error_distinction_preserved(self):
        """Real evidence of before-execution failure raises RuntimeError and preserves retry capability."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        # Path to non-existent executable
        nonexistent = os.path.join(td, "no_such_binary")
        bridge = MailboxBridge(work, aplexer_bin=nonexistent, state_dir=state_dir)
        hub_msg = {"id": "hub-before-exec", "to_actor": "agent-1", "body": "hello", "kind": "note"}

        with self.assertRaises(RuntimeError) as ctx:
            bridge.forward_hub_to_native(hub_msg)
        self.assertIn("failed before execution", str(ctx.exception).lower())
        self.assertNotIn("hub-before-exec", bridge.receipts.get("ambiguous_sends", {}))
        self.assertNotIn("hub-before-exec", bridge.receipts.get("hub_to_native", {}))

    def test_bf2_deliver_unknown_status_zero_byte_attempts_and_retained_evidence(self):
        """Unfamiliar/unknown status in delivers ledger refuses redelivery with zero attempts and retains evidence."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        bridge = MailboxBridge(work, state_dir=state_dir)
        native_id = "native-unknown-stat-1"
        hub_id = "hub-unknown-stat-1"
        bridge.receipts["hub_to_native"][hub_id] = native_id
        bridge.receipts["hub_messages"][native_id] = {
            "from_actor": "lead",
            "to_actor": "agent-1",
            "hub_id": hub_id,
            "recipient_session_id": rec_uuid,
            "kind": "sync_reply",
        }
        # In-progress / version-skew out-of-band status
        bridge.receipts["delivers"][native_id] = {
            "hub_id": hub_id,
            "status": "in_progress",
            "prior_evidence": "must_not_be_erased",
            "attempted_at": 1700000000,
        }
        bridge._save_receipts()

        hub_msg = {
            "id": hub_id,
            "to_actor": "agent-1",
            "from_actor": "lead",
            "body": "sync work",
            "kind": "sync_reply",
        }

        with patch("subprocess.run") as mock_run:
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("unfamiliar/unknown delivery state", str(ctx.exception).lower())
            # Zero byte/deliver attempts: composer status/capture and deliver never executed
            mock_run.assert_not_called()

        # Verify prior evidence was NOT overwritten by "intent"
        deliv = bridge.receipts["delivers"][native_id]
        self.assertEqual(deliv["status"], "in_progress")
        self.assertEqual(deliv["prior_evidence"], "must_not_be_erased")

    def test_bf2_malformed_corrupt_receipts_failclosed_no_overwrite_no_replay(self):
        """Malformed or corrupt receipts file fails closed immediately; zero file overwrite and zero replay."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        os.makedirs(work, exist_ok=True)
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        receipts_file = os.path.join(work, ".local", "hub-bridge-receipts.json")
        os.makedirs(os.path.dirname(receipts_file), exist_ok=True)
        corrupt_content = b'{"hub_to_native": {"incomplete": json_syntax_error'
        with open(receipts_file, "wb") as f:
            f.write(corrupt_content)
        os.chmod(receipts_file, 0o600)

        initial_sha = hashlib.sha256(corrupt_content).hexdigest()

        # 1. Instantiation fails closed on corrupt file
        with self.assertRaises(BridgeCapabilityGap) as ctx:
            MailboxBridge(work, state_dir=state_dir)
        self.assertIn("corrupt or unreadable", str(ctx.exception).lower())

        # Verify file on disk was NOT overwritten
        with open(receipts_file, "rb") as f:
            current_content = f.read()
        self.assertEqual(hashlib.sha256(current_content).hexdigest(), initial_sha)

        # 2. Non-dict root also fails closed
        with open(receipts_file, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)

        with self.assertRaises(BridgeCapabilityGap) as ctx2:
            MailboxBridge(work, state_dir=state_dir)
        self.assertIn("root must be a dict", str(ctx2.exception).lower())

    def test_valid_once_and_restart_paths_no_liveness_regression(self):
        """Valid send and sync_reply deliver execute once and succeed across restarts without regression."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, "agent-1", rec_uuid)

        bridge = MailboxBridge(work, state_dir=state_dir)

        # 1. Note send: executes once, caches, and succeeds on restart
        note_msg = {"id": "hub-live-note", "to_actor": "agent-1", "body": "hello note", "kind": "note"}
        valid_send_out = json.dumps({
            "schema_version": 1,
            "id": "native-live-note",
            "workspace": canonical_ws,
            "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
            "to": {"tag": "agent-1", "session_id": rec_uuid},
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=valid_send_out)
            nid1 = bridge.forward_hub_to_native(note_msg)
            self.assertEqual(nid1, "native-live-note")
            self.assertEqual(mock_run.call_count, 1)

            # In-memory replay: 0 calls
            nid2 = bridge.forward_hub_to_native(note_msg)
            self.assertEqual(nid2, "native-live-note")
            self.assertEqual(mock_run.call_count, 1)

        # Restart bridge
        bridge_restarted = MailboxBridge(work, state_dir=state_dir)
        with patch("subprocess.run") as mock_run:
            nid3 = bridge_restarted.forward_hub_to_native(note_msg)
            self.assertEqual(nid3, "native-live-note")
            mock_run.assert_not_called()

        # 2. Sync reply deliver: executes once, caches, and succeeds on restart
        sync_msg = {"id": "hub-live-sync", "to_actor": "agent-1", "body": "wake", "kind": "sync_reply"}
        valid_sync_send_out = json.dumps({
            "schema_version": 1,
            "id": "native-live-sync",
            "workspace": canonical_ws,
            "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
            "to": {"tag": "agent-1", "session_id": rec_uuid},
        })

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=valid_sync_send_out),  # send
                MagicMock(returncode=0, stdout='{"state":"idle"}'),    # obs 1 status
                MagicMock(returncode=0, stdout='prompt ❯'),           # obs 1 capture
                MagicMock(returncode=0, stdout='{"state":"idle"}'),    # obs 2 status
                MagicMock(returncode=0, stdout='prompt ❯'),           # obs 2 capture
                MagicMock(returncode=0, stdout='{"state":"idle"}'),    # final check status
                MagicMock(returncode=0, stdout='prompt ❯'),           # final check capture
                MagicMock(returncode=0, stdout='{"id":"native-live-sync"}'),  # deliver
            ]
            deliv_id = bridge.forward_hub_to_native(sync_msg)
            self.assertEqual(deliv_id, "native-live-sync")

        self.assertEqual(bridge.receipts["delivers"]["native-live-sync"]["status"], "delivered")

        # In-memory and restart replay: 0 calls
        with patch("subprocess.run") as mock_run:
            deliv_replay = bridge.forward_hub_to_native(sync_msg)
            self.assertEqual(deliv_replay, "native-live-sync")
            mock_run.assert_not_called()

        bridge_restarted_2 = MailboxBridge(work, state_dir=state_dir)
        with patch("subprocess.run") as mock_run:
            deliv_restart_replay = bridge_restarted_2.forward_hub_to_native(sync_msg)
            self.assertEqual(deliv_restart_replay, "native-live-sync")
            mock_run.assert_not_called()


class TestBridgeAddressingAndComposer(unittest.TestCase):
    """GAP-10: exact hub address -> native tag mapping and a strict empty-composer check."""

    def _bridge_with_state(self, tag="agent-1"):
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        canonical_ws = canonicalize_workspace(work)
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(state_dir, canonical_ws, tag, rec_uuid)
        bridge = MailboxBridge(work, state_dir=state_dir, runtime_dir=os.path.join(td, "runtime"))
        send_out = json.dumps({
            "schema_version": 1,
            "id": "nid-1",
            "workspace": canonical_ws,
            "from": {"tag": bridge.service_binding.tag, "session_id": bridge.service_binding.session_id},
            "to": {"tag": tag, "session_id": rec_uuid},
        })
        return bridge, send_out

    def test_native_tag_for_address_exact_mapping(self):
        self.assertEqual(native_tag_for_address("agent-1"), "agent-1")
        self.assertEqual(native_tag_for_address("projA:agent-1"), "agent-1")
        self.assertEqual(native_tag_for_address("agent:agent-1"), "agent-1")  # parsed as project 'agent'
        for address in ["role:principal", "machine:m1", "projA:agent-1:x", "projA:", ":agent-1", "a::b",
                        "", " agent-1", "agent 1", "a\tb", "x" * 129, None, 7]:
            self.assertIsNone(native_tag_for_address(address), address)

    def test_composer_is_empty_only_for_bare_prompt(self):
        for line in ["❯", ">", "$", "#", "  ❯  ", "│ > │", "│ ❯                    │", "┃ $ ┃",
                     "prompt ❯", "user@host:~$ ❯ "]:
            self.assertTrue(composer_is_empty(line), line)
        for line in ["echo hi >", "ls -la $", "draft about #", "❯ half draft", "│ > draft │", "prompt ❯ typed",
                     "two words ❯", "❯ x ❯", "> ❯", "x >", "x $", "x #", ">>", "", "│ │", "user@host:~$", "abc❯"]:
            self.assertFalse(composer_is_empty(line), line)

    def _sync_attempt(self, bridge, send_out, status, capture):
        hub_msg = {"id": f"sync-{uuid.uuid4()}", "to_actor": "agent-1", "body": "wake", "kind": "sync_reply"}
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=send_out),
                MagicMock(returncode=0, stdout=json.dumps({"state": status})),
                MagicMock(returncode=0, stdout=capture),
            ] + [MagicMock(returncode=0, stdout=json.dumps({"state": status})), MagicMock(returncode=0, stdout=capture)] * 2 + [
                MagicMock(returncode=0, stdout='{"id":"nid-1"}'),
            ]
            try:
                bridge.forward_hub_to_native(hub_msg)
                delivered = True
            except BridgeCapabilityGap:
                delivered = False
            deliver_calls = [c for c in mock_run.call_args_list if "deliver" in c.args[0]]
        bridge.receipts = {}
        return delivered, deliver_calls

    def test_sync_reply_refuses_drafts_ending_in_prompt_chars_and_non_idle_states(self):
        bridge, send_out = self._bridge_with_state()
        for capture in ["welcome\necho hi >", "welcome\nls -la $", "welcome\nnote #", "welcome\nfix the bug ❯", "welcome\n│ > draft │"]:
            delivered, deliver_calls = self._sync_attempt(bridge, send_out, "idle", capture)
            self.assertFalse(delivered, capture)
            self.assertEqual(deliver_calls, [], capture)
        for status in ["busy", "unknown", "frozen", "waiting", "exited"]:
            delivered, deliver_calls = self._sync_attempt(bridge, send_out, status, "welcome\n❯")
            self.assertFalse(delivered, status)
            self.assertEqual(deliver_calls, [], status)
        # Idle with a bare glyph inside composer borders is the only path that types
        delivered, deliver_calls = self._sync_attempt(bridge, send_out, "idle", "welcome\n│ > │")
        self.assertTrue(delivered)
        self.assertEqual(len(deliver_calls), 1)

    def test_forward_refuses_addresses_without_native_mailbox_before_any_effect(self):
        bridge, _ = self._bridge_with_state()
        for to_actor in ["role:principal", "machine:m1", "projA:agent-1:x", "projA:", ""]:
            with patch("subprocess.run") as mock_run:
                with self.assertRaises(BridgeCapabilityGap):
                    bridge.forward_hub_to_native({"id": f"h-{to_actor}", "to_actor": to_actor, "body": "b", "kind": "note"})
                mock_run.assert_not_called()
            self.assertNotIn(f"h-{to_actor}", bridge.receipts.get("ambiguous_sends", {}))
        # A message whose address names another tag is refused even when polled for agent-1
        with patch("subprocess.run") as mock_run:
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native({"id": "h-other", "to_actor": "projA:agent-2", "body": "b", "kind": "note"}, native_tag="agent-1")
            mock_run.assert_not_called()

    def test_forward_role_alias_uses_polled_native_tag(self):
        bridge, send_out = self._bridge_with_state()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=send_out)
            nid = bridge.forward_hub_to_native(
                {"id": "h-role", "to_actor": "role:principal", "from_actor": "projB:lead", "body": "b", "kind": "note"},
                native_tag="agent-1",
            )
        self.assertEqual(nid, "nid-1")
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--to") + 1], "agent-1")
        self.assertEqual(bridge.receipts["hub_messages"]["nid-1"]["native_tag"], "agent-1")
        self.assertEqual(bridge.receipts["hub_messages"]["nid-1"]["to_actor"], "role:principal")

    def test_native_kind_normalization(self):
        for kind in ["note", "handoff", "reply", "status", "claim", "release"]:
            self.assertEqual(normalize_native_kind(kind), kind)
        for kind in ["sync_reply", "scope_claim", "question", "", None, 3]:
            self.assertEqual(normalize_native_kind(kind), "reply")

    def test_registered_bridge_targets(self):
        actors = ["machine:m1", "agent-1", "projA:agent-1", "projB:agent-2", "role:principal", "bad:a:b"]
        self.assertEqual(registered_bridge_targets(actors), [("projA:agent-1", "agent-1"), ("projB:agent-2", "agent-2")])
        self.assertEqual(registered_bridge_targets(actors, ["agent-2"]), [("projB:agent-2", "agent-2")])
        with self.assertRaises(BridgeCapabilityGap):
            registered_bridge_targets(actors, ["agent-3"])
        self.assertEqual(registered_bridge_targets({"error": "x"}), [])


class _FakeInbox:
    """bus.inbox with the hub's paging: rows ordered by id, only `id > cursor`, at most `limit`."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, actor, limit=50, cursor=None):
        self.calls.append((actor, limit, cursor))
        rows = sorted((r for r in self.rows if cursor is None or r["id"] > cursor), key=lambda r: r["id"])
        return [dict(r) for r in rows[:limit]]


class TestBridgePollCursor(unittest.TestCase):
    """F2/F3/F7: durable inbox cursor, bounded window errors, no duplicate forward, surfaced sync errors."""

    def setUp(self):
        td = get_test_tmp_dir()
        self.work = os.path.join(td, "work")
        self.state_dir = os.path.join(td, "state")
        os.makedirs(self.work, exist_ok=True)
        self.bridge = MailboxBridge(self.work, state_dir=self.state_dir)
        self.forward_calls = []
        self.failures = {}

    def _messages(self, n, created=None):
        created = created or [1000 + i for i in range(n)]
        return [{"id": f"m{i:03d}", "to_actor": "projA:agent-1", "from_actor": "projB:lead", "kind": "note",
                 "body": f"b{i}", "created_at": created[i]} for i in range(n)]

    def _forward(self, msg, native_tag=None):
        self.forward_calls.append(msg["id"])
        error = self.failures.get(msg["id"])
        if error:
            raise error
        self.bridge.receipts["hub_to_native"][msg["id"]] = f"n-{msg['id']}"
        return f"n-{msg['id']}"

    def _poll(self, inbox, limit=50, max_pages=10, synced=None):
        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch.object(self.bridge, "forward_hub_to_native", side_effect=self._forward), \
             patch.object(self.bridge, "sync_native_to_hub", return_value=synced or {"errors": [], "held": []}):
            mock_bus.inbox.side_effect = inbox
            return poll_once(self.bridge, [("projA:agent-1", "agent-1")], limit=limit, max_pages=max_pages)

    def test_permanently_held_head_does_not_starve_third_message_across_three_cycles(self):
        # Reviewer countercase: production poll_once, three rows, one page of two per cycle.
        inbox = _FakeInbox(self._messages(3))
        self.failures = {"m000": BridgeCapabilityGap("persistent refusal")}
        cycles = [self._poll(inbox, limit=2, max_pages=1) for _ in range(3)]
        self.assertEqual(self.forward_calls.count("m001"), 1)
        self.assertEqual(self.forward_calls.count("m002"), 1)
        self.assertTrue(all(c["held"] for c in cycles))
        self.assertNotIn("m000", self.bridge.receipts["hub_to_native"])
        self.assertNotIn("m000", self.bridge.receipts["acked_hub_messages"])

    def test_acked_and_already_forwarded_messages_are_never_forwarded_again(self):
        rows = self._messages(3)
        rows[0]["acked_at"] = 5
        self.bridge.receipts["hub_to_native"]["m001"] = "n-old"
        summary = self._poll(_FakeInbox(rows))
        self.assertEqual(self.forward_calls, ["m002"])
        self.assertEqual((summary["acked_skipped"], summary["already_forwarded"]), (1, 1))
        self.assertEqual([f["hub_id"] for f in summary["forwarded"]], ["m002"])
        # Same inbox again from the start: still no second forward of any of them
        self.bridge.receipts["inbox_cursors"] = {}
        self.bridge._save_receipts()
        summary = self._poll(_FakeInbox(rows))
        self.assertEqual(self.forward_calls, ["m002"])
        self.assertEqual((summary["acked_skipped"], summary["already_forwarded"], summary["forwarded"]), (1, 2, []))

    def test_window_exhaustion_is_an_error_and_the_next_cycle_continues_from_the_cursor(self):
        inbox = _FakeInbox(self._messages(13))
        first = self._poll(inbox, limit=1, max_pages=10)
        self.assertEqual(len(first["forwarded"]), 10)
        self.assertEqual(len(first["errors"]), 1)
        self.assertIn("inbox window exhausted", first["errors"][0]["error"])
        self.assertEqual(self.bridge.inbox_cursor("projA:agent-1"), "m008")

        second = self._poll(inbox, limit=1, max_pages=10)
        self.assertEqual(inbox.calls[10], ("projA:agent-1", 1, "m009"))
        self.assertEqual([f["hub_id"] for f in second["forwarded"]], ["m010", "m011", "m012"])
        self.assertEqual((second["already_forwarded"], second["errors"]), (0, []))
        self.assertEqual(sorted(self.forward_calls), [f"m{i:03d}" for i in range(13)])
        self.assertEqual(self.bridge.inbox_cursor("projA:agent-1"), "m011")

    def test_held_or_failed_messages_are_pending_while_the_cursor_progresses(self):
        inbox = _FakeInbox(self._messages(5))
        self.failures = {"m001": BridgeCapabilityGap("not idle"), "m002": RuntimeError("send failed before execution")}
        first = self._poll(inbox)
        self.assertEqual([f["hub_id"] for f in first["forwarded"]], ["m000", "m003", "m004"])
        self.assertEqual([h["hub_id"] for h in first["held"]], ["m001"])
        self.assertEqual([e["hub_id"] for e in first["errors"] if "hub_id" in e], ["m002"])
        self.assertEqual(self.bridge.inbox_cursor("projA:agent-1"), "m003")
        self.assertEqual(set(self.bridge.pending_inbox("projA:agent-1")), {"m001", "m002"})

        self.failures = {}
        second = self._poll(inbox)
        self.assertEqual(inbox.calls[-1][2], "m003")
        self.assertEqual([f["hub_id"] for f in second["forwarded"]], ["m001", "m002"])
        self.assertEqual(second["already_forwarded"], 1)
        self.assertEqual(self.bridge.inbox_cursor("projA:agent-1"), "m003")
        self.assertEqual({i: self.forward_calls.count(i) for i in set(self.forward_calls)},
                         {"m000": 1, "m001": 2, "m002": 2, "m003": 1, "m004": 1})

    def test_retry_and_scan_survive_restart_and_single_row_windows(self):
        inbox = _FakeInbox(self._messages(4, created=[7, 7, 7, 8]))
        self.failures = {"m000": BridgeCapabilityGap("busy"), "m001": RuntimeError("unavailable")}
        self._poll(inbox, limit=1, max_pages=1)
        self.bridge = MailboxBridge(self.work, state_dir=self.state_dir)
        self.assertEqual(set(self.bridge.pending_inbox("projA:agent-1")), {"m000"})
        for _ in range(4):
            self._poll(inbox, limit=1, max_pages=1)
            self.bridge = MailboxBridge(self.work, state_dir=self.state_dir)
        self.assertEqual(self.forward_calls.count("m002"), 1)
        self.assertEqual(self.forward_calls.count("m003"), 1)
        self.assertEqual(set(self.bridge.pending_inbox("projA:agent-1")), {"m000", "m001"})
        self.failures = {}
        done = self._poll(inbox)
        self.assertEqual({f["hub_id"] for f in done["forwarded"]}, {"m000", "m001"})
        self.assertEqual(self.bridge.pending_inbox("projA:agent-1"), {})
        self.assertEqual(self.bridge.receipts["acked_hub_messages"], [])

    def test_pending_capacity_stops_without_loss_and_expiry_releases_capacity(self):
        inbox = _FakeInbox(self._messages(3))
        inbox.rows[0]["expires_at"] = 2000000000000
        self.failures = {"m000": BridgeCapabilityGap("busy"), "m001": BridgeCapabilityGap("busy")}
        with patch("agent_hub_host.bridge.MAX_PENDING_INBOX", 1):
            first = self._poll(inbox, limit=2, max_pages=1)
            self.assertIn("capacity exhausted", first["errors"][0]["error"])
            self.assertEqual(self.bridge.inbox_scan("projA:agent-1"), "m000")
            self.assertEqual(set(self.bridge.pending_inbox("projA:agent-1")), {"m000"})
            # Explicit expiry, never size-based eviction or successful ACK fabrication.
            with patch("agent_hub_host.bridge.time.time", return_value=2000000001):
                self._poll(inbox, limit=1, max_pages=1)
            self.assertEqual(set(self.bridge.pending_inbox("projA:agent-1")), {"m001"})
            self.assertNotIn("m000", self.bridge.receipts["hub_to_native"])
            self.assertEqual(self.bridge.receipts["acked_hub_messages"], [])

    def test_cached_pending_ack_is_not_replayed_after_native_receipt_pruning(self):
        row = self._messages(1)[0]
        self.bridge.remember_inbox("projA:agent-1", row)
        self.bridge.receipts["acked_hub_messages"].append(row["id"])
        self.bridge._save_receipts()
        self.bridge = MailboxBridge(self.work, state_dir=self.state_dir)
        outcome = self._poll(_FakeInbox([]))
        self.assertEqual(self.forward_calls, [])
        self.assertEqual(outcome["acked_skipped"], 1)
        self.assertEqual(self.bridge.pending_inbox("projA:agent-1"), {})

    def test_scan_returns_to_timestamp_fence_for_late_same_millisecond_row(self):
        inbox = _FakeInbox(self._messages(3, created=[1, 2, 2]))
        for _ in range(4):
            self._poll(inbox, limit=1, max_pages=1)
        late = dict(inbox.rows[1], id="m001a")
        inbox.rows.append(late)
        self._poll(inbox)
        self.assertEqual(self.forward_calls.count("m001a"), 1)
        self.assertEqual(self.forward_calls.count("m001"), 1)
        self.assertEqual(self.forward_calls.count("m002"), 1)

    def test_cursor_stays_below_the_newest_millisecond_and_needs_created_at(self):
        self._poll(_FakeInbox(self._messages(3, created=[7, 7, 7])))
        self.assertIsNone(self.bridge.inbox_cursor("projA:agent-1"))
        rows = self._messages(2)
        for r in rows:
            del r["created_at"]
        self._poll(_FakeInbox(rows))
        self.assertIsNone(self.bridge.inbox_cursor("projA:agent-1"))
        self._poll(_FakeInbox(self._messages(3, created=[1, 2, 2])))
        self.assertEqual(self.bridge.inbox_cursor("projA:agent-1"), "m000")

    def test_cursor_is_durable_and_only_moves_forward(self):
        self.bridge.advance_inbox_cursor("projA:agent-1", "m005")
        other = MailboxBridge(self.work, state_dir=self.state_dir)
        self.assertEqual(other.inbox_cursor("projA:agent-1"), "m005")
        other.advance_inbox_cursor("projA:agent-1", "m003")
        self.assertEqual(other.inbox_cursor("projA:agent-1"), "m005")
        self.assertEqual(MailboxBridge(self.work, state_dir=self.state_dir).inbox_cursor("projA:agent-1"), "m005")
        self.bridge.advance_inbox_cursor("projB:agent-2", "m001")
        reloaded = MailboxBridge(self.work, state_dir=self.state_dir)
        self.assertEqual(reloaded.receipts["inbox_cursors"], {"projA:agent-1": "m005", "projB:agent-2": "m001"})
        with open(reloaded.receipts_file, "w", encoding="utf-8") as f:
            json.dump({"inbox_cursors": ["bad"]}, f)
        with self.assertRaises(BridgeCapabilityGap):
            MailboxBridge(self.work, state_dir=self.state_dir)

    def test_inbox_errors_and_sync_errors_reach_the_summary(self):
        def broken(actor, limit=50, cursor=None):
            raise ValueError("HTTP request failed with status 500")
        synced = {"errors": [{"hub_id": "h1", "error": "hub ack failed: 403"}], "held": [{"native_id": "x", "reason": "reply refused"}]}
        summary = self._poll(broken, synced=synced)
        self.assertEqual([e.get("address") or e.get("hub_id") for e in summary["errors"]], ["projA:agent-1", "h1"])
        self.assertEqual(summary["held"], [{"native_id": "x", "reason": "reply refused"}])
        summary = self._poll(lambda actor, limit=50, cursor=None: {"error": "x"})
        self.assertEqual(summary["errors"], [{"address": "projA:agent-1", "error": "inbox failed: inbox did not return a list"}])

    def test_sync_native_to_hub_returns_hub_ack_and_native_log_failures(self):
        canonical_ws = canonicalize_workspace(self.work)
        native_id = str(uuid.uuid4())
        create_authoritative_state(self.state_dir, canonical_ws, "agent-1", str(uuid.uuid4()), exceptions=[native_id])
        self.bridge.receipts["hub_to_native"] = {"hub-1": native_id}
        self.bridge.receipts["hub_messages"] = {native_id: {"from_actor": "alice", "to_actor": "agent-1", "hub_id": "hub-1"}}
        with patch("agent_hub_host.bridge.bus") as mock_bus, \
             patch("subprocess.run", return_value=MagicMock(returncode=2, stdout="", stderr="log broke")):
            mock_bus.ack.side_effect = ValueError("HTTP request failed with status 403")
            outcome = self.bridge.sync_native_to_hub()
        self.assertEqual(outcome["held"], [])
        self.assertEqual(outcome["errors"][0], {"hub_id": "hub-1", "error": "hub ack failed: HTTP request failed with status 403"})
        self.assertIn("aplexer message log failed (exit 2)", outcome["errors"][1]["error"])
        self.assertNotIn("hub-1", self.bridge.receipts["acked_hub_messages"])
        for stdout, expected in (("not json", "is not JSON"), ('{"a": 1}', "is not a JSON list")):
            with patch("agent_hub_host.bridge.bus"), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=stdout)):
                self.assertIn(expected, self.bridge.sync_native_to_hub()["errors"][-1]["error"])


class _LoopbackHub:
    """Local mocked hub API on 127.0.0.1 with the M3 delivered-before-ACK rule and inbox paging."""

    def __init__(self, actors, messages, post_status=None):
        import http.server
        import threading

        self.actors = actors
        self.messages = {m["id"]: dict(m, delivered_at=None, acked_at=None) for m in messages}
        self.requests = []
        self.post_status = post_status
        hub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                hub.requests.append(("GET", self.path, None, self.headers.get("Authorization")))
                from urllib.parse import urlparse, parse_qs
                url = urlparse(self.path)
                if url.path == "/api/bus/actors":
                    return self._json(200, hub.actors)
                if url.path == "/api/bus/inbox":
                    query = parse_qs(url.query)
                    actor = query.get("actor", [""])[0]
                    cursor = query.get("cursor", [""])[0]
                    limit = int(query.get("limit", ["50"])[0])
                    tag = actor.split(":")[-1]
                    rows = sorted((m for m in hub.messages.values() if m["to_actor"] in (actor, tag) and m["id"] > cursor),
                                  key=lambda m: m["id"])[:limit]
                    for m in rows:
                        m["delivered_at"] = m["delivered_at"] or 1
                    return self._json(200, rows)
                return self._json(404, {"error": "not_found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
                hub.requests.append(("POST", self.path, body, self.headers.get("Authorization")))
                if hub.post_status:
                    return self._json(hub.post_status, {"error": "forbidden", "reason": "scope_missing"})
                parts = self.path.strip("/").split("/")
                if parts[:3] == ["api", "bus", "messages"] and parts[-1] == "ack":
                    m = hub.messages.get(parts[3])
                    if not m:
                        return self._json(404, {"error": "not_found"})
                    if not m["delivered_at"]:
                        return self._json(400, {"error": "invalid_stage"})
                    m["acked_at"] = 2
                    return self._json(200, {"status": "ok", "id": parts[3], "stage": "ack"})
                if self.path == "/api/bus/send":
                    return self._json(200, {"id": "hub-reply-1", "thread_id": body.get("thread_id")})
                return self._json(404, {"error": "not_found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


class TestBridgeCliLoopback(unittest.TestCase):
    """GAP-2: `hub bridge` CLI against fake-a and a loopback mocked hub (no real native or hub)."""

    def setUp(self):
        td = get_test_tmp_dir()
        self.td = td
        self.work = os.path.join(td, "work")
        self.state_dir = os.path.join(td, "native-state")
        self.runtime_dir = os.path.join(td, "native-runtime")
        os.makedirs(self.work, exist_ok=True)
        os.makedirs(self.runtime_dir, exist_ok=True)
        canonical_ws = canonicalize_workspace(self.work)
        self.native_msg_id = str(uuid.uuid4())
        self.native_reply_id = str(uuid.uuid4())
        rec_uuid = str(uuid.uuid4())
        create_authoritative_state(self.state_dir, canonical_ws, "agent-1", rec_uuid, exceptions=[self.native_msg_id])
        self.calls_log = os.path.join(td, "fake-a-calls.jsonl")
        self.fake_a = os.path.join(td, "fake-a")
        with open(self.fake_a, "w", encoding="utf-8") as f:
            f.write(f'''#!/usr/bin/env python3
import sys, json
args = sys.argv[1:]
with open({self.calls_log!r}, "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["message", "send"]:
    print(json.dumps({{
        "schema_version": 1, "id": "{self.native_msg_id}", "workspace": "{canonical_ws}",
        "from": {{"tag": "hub-bridge", "session_id": "00000000-0000-0000-0000-000000000001", "workspace": "{canonical_ws}"}},
        "to": {{"tag": args[args.index("--to") + 1], "session_id": "{rec_uuid}"}},
        "kind": "note", "reply_to": None, "body": args[-1], "delivery": "inbox"
    }}))
elif args[:2] == ["message", "log"]:
    print(json.dumps([{{
        "schema_version": 1, "id": "{self.native_reply_id}", "workspace": "{canonical_ws}",
        "from": {{"tag": "agent-1", "session_id": "{rec_uuid}", "workspace": "{canonical_ws}"}},
        "to": {{"tag": "hub-bridge"}}, "kind": "sync_reply", "reply_to": "{self.native_msg_id}",
        "body": "done by fake-a", "delivery": "inbox"
    }}]))
else:
    sys.exit(3)
''')
        os.chmod(self.fake_a, 0o755)

    def _run_cli(self, hub_url, *extra):
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env.update({
            "HUB_URL": hub_url,
            "HUB_TOKEN": "test-host-token",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
            "HOME": self.td,
            "XDG_CONFIG_HOME": os.path.join(self.td, "config"),
            "XDG_RUNTIME_DIR": self.runtime_dir,
            "APLEXER_STATE_DIR": self.state_dir,
            "APLEXER_RUNTIME_DIR": self.runtime_dir,
        })
        env.pop("HUB_JOB_TOKEN", None)
        env.pop("HUB_LEASE_EPOCH", None)
        host_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        cmd = [sys.executable, "-m", "agent_hub_host.cli", "bridge", "--workspace", self.work,
               "--aplexer-bin", self.fake_a, "--state-dir", self.state_dir, "--runtime-dir", self.runtime_dir, *extra]
        return subprocess.run(cmd, cwd=host_dir, env=env, capture_output=True, text=True, timeout=60)

    def _fake_a_calls(self):
        if not os.path.exists(self.calls_log):
            return []
        with open(self.calls_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_cli_bridge_once_round_trip(self):
        msg = {"id": "hub-1", "from_actor": "projB:lead", "to_actor": "projA:agent-1", "kind": "note",
               "body": "do the task", "thread_id": "th-1"}
        with _LoopbackHub(["machine:m1", "projA:agent-1"], [msg]) as hub:
            res = self._run_cli(hub.url, "--once")
            self.assertEqual(res.returncode, 0, res.stderr)
            out = json.loads(res.stdout)
            self.assertEqual(out["targets"], [{"address": "projA:agent-1", "tag": "agent-1"}])
            self.assertEqual(out["cycles"][0]["forwarded"], [{"hub_id": "hub-1", "native_id": self.native_msg_id, "tag": "agent-1"}])

            sends = [c for c in self._fake_a_calls() if c[:2] == ["message", "send"]]
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0][sends[0].index("--to") + 1], "agent-1")
            self.assertNotIn("deliver", [c[1] for c in self._fake_a_calls() if len(c) > 1])

            posts = [(p, b) for m, p, b, _ in hub.requests if m == "POST"]
            self.assertIn(("/api/bus/messages/hub-1/ack", None), posts)
            self.assertEqual(hub.messages["hub-1"]["acked_at"], 2)
            replies = [b for p, b in posts if p == "/api/bus/send"]
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["from_actor"], "projA:agent-1")
            self.assertEqual(replies[0]["to_actor"], "projB:lead")
            self.assertEqual(replies[0]["reply_to"], "hub-1")
            self.assertEqual(replies[0]["kind"], "reply")  # native sync_reply normalized, never sent upstream
            self.assertEqual(replies[0]["idempotency_key"], self.native_reply_id)
            self.assertTrue(all(auth == "Bearer test-host-token" for *_, auth in hub.requests))

            # A second bounded run is a no-op: no second native send, no second reply
            res2 = self._run_cli(hub.url, "--cycles", "1")
            self.assertEqual(res2.returncode, 0, res2.stderr)
            self.assertEqual(len([c for c in self._fake_a_calls() if c[:2] == ["message", "send"]]), 1)
            self.assertEqual(len([1 for m, p, *_ in hub.requests if m == "POST" and p == "/api/bus/send"]), 1)

    def test_cli_bridge_exits_nonzero_when_hub_ack_and_reply_fail(self):
        msg = {"id": "hub-3", "from_actor": "projB:lead", "to_actor": "projA:agent-1", "kind": "note",
               "body": "do it", "thread_id": "th-3"}
        with _LoopbackHub(["machine:m1", "projA:agent-1"], [msg], post_status=403) as hub:
            res = self._run_cli(hub.url, "--once")
            self.assertEqual(res.returncode, 1, res.stderr)
            errors = json.loads(res.stdout)["cycles"][0]["errors"]
            self.assertEqual([e.get("hub_id") for e in errors], ["hub-3", "hub-3"])
            self.assertIn("hub ack failed", errors[0]["error"])
            self.assertIn("hub reply failed", errors[1]["error"])
            self.assertEqual(errors[1]["native_id"], self.native_reply_id)
            self.assertIsNone(hub.messages["hub-3"]["acked_at"])

            # The failures are retried, not lost: the next run tries ack and reply again, with no second native send
            posts_before = len([1 for m, *_ in hub.requests if m == "POST"])
            hub.post_status = None
            res2 = self._run_cli(hub.url, "--once")
            self.assertEqual(res2.returncode, 0, res2.stderr)
            self.assertEqual(json.loads(res2.stdout)["cycles"][0]["errors"], [])
            self.assertEqual(hub.messages["hub-3"]["acked_at"], 2)
            self.assertEqual(len([1 for m, *_ in hub.requests if m == "POST"]), posts_before + 2)
            self.assertEqual(len([c for c in self._fake_a_calls() if c[:2] == ["message", "send"]]), 1)

    def test_cli_bridge_refuses_unregistered_tag_and_unbounded_runs(self):
        msg = {"id": "hub-2", "from_actor": "projB:lead", "to_actor": "projA:agent-1", "kind": "note", "body": "b"}
        with _LoopbackHub(["machine:m1", "projA:agent-1"], [msg]) as hub:
            res = self._run_cli(hub.url, "--tag", "agent-9")
            self.assertEqual(res.returncode, 2)
            self.assertIn("not registered", res.stderr)
            for bad in (["--cycles", "0"], ["--cycles", "1001"], ["--limit", "0"], ["--max-pages", "0"], ["--max-pages", "101"]):
                self.assertEqual(self._run_cli(hub.url, *bad).returncode, 2, bad)
            self.assertFalse(any(p.startswith("/api/bus/inbox") for _, p, *_ in hub.requests))
        self.assertEqual(self._fake_a_calls(), [])

    def test_cli_bridge_refuses_host_without_registered_tags(self):
        with _LoopbackHub(["machine:m1"], []) as hub:
            res = self._run_cli(hub.url)
            self.assertEqual(res.returncode, 2)
            self.assertIn("no agent tags", res.stderr)
        self.assertEqual(self._fake_a_calls(), [])


if __name__ == "__main__":
    unittest.main()
