"""Native Aplexer Mailbox Bridge Contract Tests (docs/SPEC.md §7.5).

Verifies strict alignment with read-only Rust aplexer messaging contracts:
- Rejection of anonymous senders (from.external=true) per Rust reply_target contract.
- Absolute FAKE native executable enforcing Rust reply_target (message_routing.rs:218-248).
- Positive bidirectional roundtrip: forward hub -> fake CLI send -> recipient reply
  through fake CLI -> fake CLI log -> Hub API reply + recipient cursor ACK.
- Foreign workspace, impostor session, and missing provenance rejection.
- Corrupt and cross-consumer cursor safety (no false ACKs).
- Exactly-once send idempotency and ambiguous send preservation.
- Honest reporting of deployed runtime capability gap without forging flags.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host.bridge import (
    BridgeCapabilityGap,
    MailboxBridge,
    canonicalize_workspace,
    get_workspace_key,
)
from agent_hub_host.native_bridge import (
    create_fake_aplexer_script,
    qualify_native_service_binding,
)


def get_test_tmp_dir() -> str:
    """Ensure temporary directories live strictly under .local/tmp, never /tmp."""
    root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", ".local", "tmp")
    )
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(dir=root)


def setup_authoritative_test_env(
    td: str,
    service_tag: str = "hub-bridge",
    recip_tag: str = "agent-1",
) -> dict[str, str]:
    """Sets up an authoritative aplexer workspace environment with verified sessions."""
    work = os.path.join(td, "work")
    state_dir = os.path.join(td, "state")
    runtime_dir = os.path.join(td, "runtime")
    fake_bin = os.path.join(td, "fake-aplexer")

    os.makedirs(work, exist_ok=True)
    canonical_ws = canonicalize_workspace(work)
    ws_key = get_workspace_key(canonical_ws)

    # 1. Fake executable
    create_fake_aplexer_script(fake_bin)

    # 2. Session records in state_dir/sessions/<id>/session.json
    service_id = str(uuid.uuid4())
    s_sdir = os.path.join(state_dir, "sessions", service_id)
    os.makedirs(s_sdir, exist_ok=True)
    service_rec = {
        "schema_version": 1,
        "id": service_id,
        "workspace": canonical_ws,
        "tag": service_tag,
        "engine": "hub-bridge",
        "created_at_ms": int(time.time() * 1000) - 5000,
        "command": ["hub-bridge-service"],
        "cwd": canonical_ws,
    }
    with open(os.path.join(s_sdir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(service_rec, f)

    recip_id = str(uuid.uuid4())
    r_sdir = os.path.join(state_dir, "sessions", recip_id)
    os.makedirs(r_sdir, exist_ok=True)
    recip_rec = {
        "schema_version": 1,
        "id": recip_id,
        "workspace": canonical_ws,
        "tag": recip_tag,
        "engine": "agent-engine",
        "created_at_ms": int(time.time() * 1000),
        "command": ["agent-cli"],
        "cwd": canonical_ws,
    }
    with open(os.path.join(r_sdir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(recip_rec, f)

    # 3. Mailbox in state_dir/messages/<ws_key>
    mailbox_dir = os.path.join(state_dir, "messages", ws_key)
    msgs_dir = os.path.join(mailbox_dir, "msgs")
    cursors_dir = os.path.join(mailbox_dir, "cursors")
    os.makedirs(msgs_dir, exist_ok=True)
    os.makedirs(cursors_dir, exist_ok=True)

    with open(os.path.join(mailbox_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"workspace": canonical_ws}, f)

    return {
        "td": td,
        "work": work,
        "canonical_ws": canonical_ws,
        "state_dir": state_dir,
        "runtime_dir": runtime_dir,
        "fake_bin": fake_bin,
        "service_id": service_id,
        "service_tag": service_tag,
        "recip_id": recip_id,
        "recip_tag": recip_tag,
        "ws_key": ws_key,
        "msgs_dir": msgs_dir,
        "cursors_dir": cursors_dir,
    }


class TestNativeBridgeContract(unittest.TestCase):

    def test_anonymous_sender_denial_and_no_false_receipt(self):
        """Unbound bridge without qualified service sender binding refuses forward with BridgeCapabilityGap."""
        td = get_test_tmp_dir()
        work = os.path.join(td, "work")
        state_dir = os.path.join(td, "state")
        fake_bin = os.path.join(td, "fake-aplexer")
        create_fake_aplexer_script(fake_bin)
        os.makedirs(work, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)

        # Bridge with empty state directory (no service session)
        bridge = MailboxBridge(work, aplexer_bin=fake_bin, state_dir=state_dir)
        self.assertIsNone(bridge.service_binding)

        hub_msg = {
            "id": "hub-msg-unbound",
            "to_actor": "agent:agent-1",
            "from_actor": "proj:lead",
            "body": "unbound test",
            "kind": "note",
        }

        with self.assertRaises(BridgeCapabilityGap) as ctx:
            bridge.forward_hub_to_native(hub_msg)

        self.assertIn("anonymous sender", str(ctx.exception).lower())
        # Ensure no false receipt persisted in hub_to_native
        self.assertNotIn("hub-msg-unbound", bridge.receipts.get("hub_to_native", {}))
        # Ensure ambiguous send evidence was recorded mode 0600 on disk
        self.assertIn("hub-msg-unbound", bridge.receipts.get("ambiguous_sends", {}))

    def test_fake_native_reply_refuses_anonymous_original(self):
        """Absolute fake aplexer binary strictly enforces Rust reply_target: anonymous originals cannot be replied to."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        fake_bin = env["fake_bin"]

        # 1. Send anonymous message using fake_bin with NO APLEXER_SESSION_ID
        send_proc = subprocess.run(
            [fake_bin, "message", "send", "--queue", "--to", env["recip_tag"], "--json", "hello anon"],
            cwd=env["work"],
            capture_output=True,
            text=True,
            env={"APLEXER_STATE_DIR": env["state_dir"], "APLEXER_WORKSPACE": env["canonical_ws"]},
            timeout=10,
        )
        self.assertEqual(send_proc.returncode, 0)
        envelope = json.loads(send_proc.stdout)
        anon_msg_id = envelope["id"]
        self.assertTrue(envelope["from"].get("external"))
        self.assertIsNone(envelope["from"].get("tag"))

        # 2. Attempt reply to anonymous message with recipient session ID
        reply_proc = subprocess.run(
            [fake_bin, "message", "reply", anon_msg_id, "--json", "trying to reply"],
            cwd=env["work"],
            capture_output=True,
            text=True,
            env={
                "APLEXER_STATE_DIR": env["state_dir"],
                "APLEXER_WORKSPACE": env["canonical_ws"],
                "APLEXER_SESSION_ID": env["recip_id"],
            },
            timeout=10,
        )
        # MUST fail with exact Rust error
        self.assertNotEqual(reply_proc.returncode, 0)
        self.assertIn(
            f"original message {anon_msg_id} was sent anonymously (no sender tag)",
            reply_proc.stderr,
        )

    def test_positive_genuine_own_fixture_sender_bidirectional_roundtrip(self):
        """End-to-end positive test: Hub -> MailboxBridge -> genuine service send -> recipient reply -> log -> Hub reply + ACK."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)

        bridge = MailboxBridge(
            env["work"],
            aplexer_bin=env["fake_bin"],
            state_dir=env["state_dir"],
            runtime_dir=env["runtime_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )
        self.assertIsNotNone(bridge.service_binding)
        self.assertEqual(bridge.service_binding.session_id, env["service_id"])
        self.assertEqual(bridge.service_binding.tag, env["service_tag"])

        hub_msg = {
            "id": "hub-task-42",
            "to_actor": "agent-1",
            "from_actor": "proj:principal",
            "body": "execute analysis",
            "kind": "note",
            "thread_id": "thread-42",
        }

        # Step 1: Forward from Hub to native
        native_id = bridge.forward_hub_to_native(hub_msg)
        self.assertTrue(bool(native_id))
        self.assertEqual(bridge.receipts["hub_to_native"]["hub-task-42"], native_id)

        # Verify on-disk message envelope has genuine service sender
        msg_file = os.path.join(env["msgs_dir"], f"{native_id}.json")
        self.assertTrue(os.path.isfile(msg_file))
        with open(msg_file, "r", encoding="utf-8") as f:
            sent_env = json.load(f)
        self.assertEqual(sent_env["from"]["tag"], env["service_tag"])
        self.assertEqual(sent_env["from"]["session_id"], env["service_id"])
        self.assertFalse(sent_env["from"].get("external", False))

        # Step 2: Native recipient replies via fake CLI (enforcing Rust reply_target)
        reply_proc = subprocess.run(
            [env["fake_bin"], "message", "reply", native_id, "--json", "analysis completed"],
            cwd=env["work"],
            capture_output=True,
            text=True,
            env={
                "APLEXER_STATE_DIR": env["state_dir"],
                "APLEXER_WORKSPACE": env["canonical_ws"],
                "APLEXER_SESSION_ID": env["recip_id"],
            },
            timeout=10,
        )
        self.assertEqual(reply_proc.returncode, 0, f"Reply failed: {reply_proc.stderr}")
        reply_env = json.loads(reply_proc.stdout)
        reply_native_id = reply_env["id"]
        self.assertEqual(reply_env["reply_to"], native_id)
        self.assertEqual(reply_env["to"]["tag"], env["service_tag"])

        # Step 3: Native recipient ACKs the original message via fake CLI
        ack_proc = subprocess.run(
            [env["fake_bin"], "message", "ack", native_id],
            cwd=env["work"],
            capture_output=True,
            text=True,
            env={
                "APLEXER_STATE_DIR": env["state_dir"],
                "APLEXER_WORKSPACE": env["canonical_ws"],
                "APLEXER_SESSION_ID": env["recip_id"],
            },
            timeout=10,
        )
        self.assertEqual(ack_proc.returncode, 0)

        # Step 4: Bridge syncs native to Hub
        with patch("agent_hub_host.bridge.bus") as mock_bus:
            mock_bus.ack.return_value = {"status": "ok"}
            mock_bus.reply.return_value = {"id": "hub-reply-999"}

            bridge.sync_native_to_hub()

            # Verify positive ACK bridged to Hub
            mock_bus.ack.assert_called_once_with("hub-task-42")
            self.assertIn("hub-task-42", bridge.receipts["acked_hub_messages"])

            # Verify reverse reply bridged to Hub with exact original actors in reverse
            mock_bus.reply.assert_called_once_with(
                "hub-task-42",
                "agent-1",
                "proj:principal",
                "reply",
                "analysis completed",
                idempotency_key=reply_native_id,
                thread_id="thread-42",
            )
            self.assertEqual(bridge.receipts["native_to_hub"][reply_native_id], "hub-reply-999")

    def test_foreign_workspace_reply_rejected(self):
        """Native replies originating from foreign workspace must be rejected."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        bridge = MailboxBridge(
            env["work"],
            state_dir=env["state_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        bridge.receipts["hub_to_native"] = {"hub-orig": "native-orig"}
        bridge.receipts["hub_messages"] = {
            "native-orig": {
                "from_actor": "proj:lead",
                "to_actor": "agent-1",
                "hub_id": "hub-orig",
                "recipient_session_id": env["recip_id"],
            }
        }
        bridge._save_receipts()

        foreign_reply_log = [
            {
                "id": "reply-foreign",
                "workspace": "/tmp/some/foreign/repo",
                "from": {
                    "session_id": env["recip_id"],
                    "tag": "agent-1",
                    "workspace": "/tmp/some/foreign/repo",
                },
                "to": {"tag": env["service_tag"]},
                "reply_to": "native-orig",
                "body": "foreign body",
                "kind": "reply",
            }
        ]

        with patch("agent_hub_host.bridge.bus") as mock_bus, patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout=json.dumps(foreign_reply_log)),
        ):
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_not_called()
            self.assertNotIn("reply-foreign", bridge.receipts.get("native_to_hub", {}))

    def test_impostor_session_reply_rejected(self):
        """Native replies from an impostor session ID pretending to be the recipient are rejected."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        bridge = MailboxBridge(
            env["work"],
            state_dir=env["state_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        bridge.receipts["hub_to_native"] = {"hub-orig": "native-orig"}
        bridge.receipts["hub_messages"] = {
            "native-orig": {
                "from_actor": "proj:lead",
                "to_actor": "agent-1",
                "hub_id": "hub-orig",
                "recipient_session_id": env["recip_id"],
            }
        }
        bridge._save_receipts()

        impostor_id = str(uuid.uuid4())
        impostor_reply_log = [
            {
                "id": "reply-impostor",
                "workspace": env["canonical_ws"],
                "from": {
                    "session_id": impostor_id,
                    "tag": "agent-1",
                    "workspace": env["canonical_ws"],
                },
                "to": {"tag": env["service_tag"]},
                "reply_to": "native-orig",
                "body": "impostor attack",
                "kind": "reply",
            }
        ]

        with patch("agent_hub_host.bridge.bus") as mock_bus, patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout=json.dumps(impostor_reply_log)),
        ):
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_not_called()
            self.assertNotIn("reply-impostor", bridge.receipts.get("native_to_hub", {}))

    def test_missing_or_external_from_reply_rejected(self):
        """Replies with from=None or from.external=True are strictly rejected."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        bridge = MailboxBridge(
            env["work"],
            state_dir=env["state_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        bridge.receipts["hub_to_native"] = {"hub-orig": "native-orig"}
        bridge.receipts["hub_messages"] = {
            "native-orig": {
                "from_actor": "proj:lead",
                "to_actor": "agent-1",
                "hub_id": "hub-orig",
                "recipient_session_id": env["recip_id"],
            }
        }
        bridge._save_receipts()

        bad_replies = [
            {"id": "r-null", "from": None, "reply_to": "native-orig", "body": "null"},
            {"id": "r-ext", "from": {"external": True}, "reply_to": "native-orig", "body": "ext"},
            {"id": "r-empty", "from": {}, "reply_to": "native-orig", "body": "empty"},
        ]

        with patch("agent_hub_host.bridge.bus") as mock_bus, patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout=json.dumps(bad_replies)),
        ):
            bridge.sync_native_to_hub()
            mock_bus.reply.assert_not_called()

    def test_corrupt_cursor_no_false_ack(self):
        """Corrupt or malformed cursor JSON does not cause false ACK."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        bridge = MailboxBridge(
            env["work"],
            state_dir=env["state_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        native_id = str(uuid.uuid4())
        bridge.receipts["hub_to_native"] = {"hub-msg": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {
                "from_actor": "user",
                "to_actor": "agent-1",
                "hub_id": "hub-msg",
                "recipient_session_id": env["recip_id"],
            }
        }
        bridge._save_receipts()

        # Write corrupted cursor file
        cpath = os.path.join(env["cursors_dir"], f"{env['recip_id']}.json")
        with open(cpath, "w", encoding="utf-8") as f:
            f.write("{broken json content")

        with patch("agent_hub_host.bridge.bus") as mock_bus, patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="[]"),
        ):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_not_called()
            self.assertNotIn("hub-msg", bridge.receipts.get("acked_hub_messages", []))

    def test_wrong_recipient_cursor_no_false_ack(self):
        """ACKs present only in another consumer's cursor do not ACK this message."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        bridge = MailboxBridge(
            env["work"],
            state_dir=env["state_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        native_id = str(uuid.uuid4())
        bridge.receipts["hub_to_native"] = {"hub-msg": native_id}
        bridge.receipts["hub_messages"] = {
            native_id: {
                "from_actor": "user",
                "to_actor": "agent-1",
                "hub_id": "hub-msg",
                "recipient_session_id": env["recip_id"],
            }
        }
        bridge._save_receipts()

        # Write cursor for wrong consumer containing native_id
        wrong_id = str(uuid.uuid4())
        wrong_cpath = os.path.join(env["cursors_dir"], f"{wrong_id}.json")
        with open(wrong_cpath, "w", encoding="utf-8") as f:
            json.dump({"exceptions": [native_id], "acked_through": None}, f)

        with patch("agent_hub_host.bridge.bus") as mock_bus, patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="[]"),
        ):
            bridge.sync_native_to_hub()
            mock_bus.ack.assert_not_called()

    def test_idempotent_retry_and_ambiguous_send_handling(self):
        """Idempotent retry reuses existing native ID; ambiguous send preserves evidence and fails closed."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        bridge = MailboxBridge(
            env["work"],
            aplexer_bin=env["fake_bin"],
            state_dir=env["state_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        hub_msg = {
            "id": "hub-dedup-test",
            "to_actor": "agent-1",
            "from_actor": "user",
            "body": "run once",
            "kind": "note",
        }

        # 1. First forward
        id1 = bridge.forward_hub_to_native(hub_msg)

        # 2. Retry forward with same hub ID reuses id1 without invoking subprocess
        with patch("subprocess.run") as mock_run:
            id2 = bridge.forward_hub_to_native(hub_msg)
            self.assertEqual(id1, id2)
            mock_run.assert_not_called()

        # 3. Simulate ambiguous send (fake send returns envelope with wrong tag provenance)
        hub_msg_amb = {
            "id": "hub-ambiguous-test",
            "to_actor": "agent-1",
            "from_actor": "user",
            "body": "ambiguous effect",
            "kind": "note",
        }

        mismatched_env = {
            "schema_version": 1,
            "id": str(uuid.uuid4()),
            "workspace": env["canonical_ws"],
            "from": {"tag": "rogue-sender", "session_id": str(uuid.uuid4())},
            "to": {"tag": "agent-1"},
            "body": "ambiguous effect",
            "kind": "note",
        }

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(mismatched_env))):
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg_amb)

        # Verify ambiguous send evidence was recorded mode 0600
        self.assertIn("hub-ambiguous-test", bridge.receipts["ambiguous_sends"])
        self.assertEqual(
            bridge.receipts["ambiguous_sends"]["hub-ambiguous-test"]["native_id"],
            mismatched_env["id"],
        )
        self.assertTrue(os.path.isfile(bridge.receipts_file))
        mode = stat.S_IMODE(os.stat(bridge.receipts_file).st_mode)
        self.assertEqual(mode, 0o600)

        # Retry of ambiguous message must refuse without resending (never erases evidence or duplicates native effect)
        with patch("subprocess.run") as mock_run:
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native(hub_msg_amb)
            self.assertIn("prior ambiguous send", str(ctx.exception))
            mock_run.assert_not_called()

    def test_fake_native_deliver_effect_then_timeout_zero_additional_writes_on_replay(self):
        """Fake native deliver simulates effect then timeout: replay executes exactly zero additional PTY writes."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        log_file = os.path.join(td, "pty_writes.log")

        bridge = MailboxBridge(
            env["work"],
            aplexer_bin=env["fake_bin"],
            state_dir=env["state_dir"],
            runtime_dir=env["runtime_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        hub_msg = {
            "id": "hub-sync-timeout",
            "to_actor": env["recip_tag"],
            "from_actor": "lead",
            "body": "sync work",
            "kind": "sync_reply",
        }

        # First attempt: deliver times out (using timeout patch on subprocess.run for deliver call)
        orig_run = subprocess.run
        deliver_invocations = []

        def timed_out_deliver_run(cmd, *args, **kwargs):
            if "deliver" in cmd:
                deliver_invocations.append(cmd)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("pty-bytes-written\n")
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            return orig_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=timed_out_deliver_run):
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("timed out", str(ctx.exception).lower())

        self.assertEqual(len(deliver_invocations), 1)

        # Retry attempt: must refuse and execute zero additional deliver/PTY writes
        with patch("subprocess.run", side_effect=timed_out_deliver_run):
            with self.assertRaises(BridgeCapabilityGap) as ctx2:
                bridge.forward_hub_to_native(hub_msg)
            self.assertIn("refusing redelivery", str(ctx2.exception).lower())

        # Assert no second invocation was made
        self.assertEqual(len(deliver_invocations), 1)
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1, "Expected exactly 1 PTY effect written across attempt and retry")

    def test_fake_native_deliver_effect_then_crash_and_restart(self):
        """Fake native deliver writes effect then crashes: restart executes zero additional writes."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        log_file = os.path.join(td, "pty_writes.log")

        bridge = MailboxBridge(
            env["work"],
            aplexer_bin=env["fake_bin"],
            state_dir=env["state_dir"],
            runtime_dir=env["runtime_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        hub_msg = {
            "id": "hub-sync-crash",
            "to_actor": env["recip_tag"],
            "from_actor": "lead",
            "body": "sync work",
            "kind": "sync_reply",
        }

        # Initial attempt with APLEXER_FAKE_DELIVER_BEHAVIOR="effect_then_crash"
        with patch.dict(os.environ, {
            "APLEXER_FAKE_DELIVER_BEHAVIOR": "effect_then_crash",
            "APLEXER_FAKE_DELIVER_LOG": log_file
        }):
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        # Verify effect was recorded in log_file
        self.assertTrue(os.path.isfile(log_file))
        with open(log_file, "r", encoding="utf-8") as f:
            writes_1 = f.readlines()
        self.assertEqual(len(writes_1), 1)

        # Restart with new bridge instance
        bridge_restarted = MailboxBridge(
            env["work"],
            aplexer_bin=env["fake_bin"],
            state_dir=env["state_dir"],
            runtime_dir=env["runtime_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        # Retry must refuse without adding any PTY writes
        with patch.dict(os.environ, {
            "APLEXER_FAKE_DELIVER_BEHAVIOR": "effect_then_crash",
            "APLEXER_FAKE_DELIVER_LOG": log_file
        }):
            with self.assertRaises(BridgeCapabilityGap) as ctx:
                bridge_restarted.forward_hub_to_native(hub_msg)
            self.assertIn("refusing redelivery", str(ctx.exception).lower())

        with open(log_file, "r", encoding="utf-8") as f:
            writes_2 = f.readlines()
        self.assertEqual(len(writes_2), 1, "Exactly zero additional deliver writes permitted after restart")

    def test_fake_native_deliver_terminal_reconciliation(self):
        """Uncertain deliver reconciles when recipient genuine cursor ACK appears; zero additional deliver writes."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)
        log_file = os.path.join(td, "pty_writes.log")

        bridge = MailboxBridge(
            env["work"],
            aplexer_bin=env["fake_bin"],
            state_dir=env["state_dir"],
            runtime_dir=env["runtime_dir"],
            service_session_id=env["service_id"],
            service_tag=env["service_tag"],
        )

        hub_msg = {
            "id": "hub-reconcile-test",
            "to_actor": env["recip_tag"],
            "from_actor": "lead",
            "body": "sync work",
            "kind": "sync_reply",
        }

        with patch.dict(os.environ, {
            "APLEXER_FAKE_DELIVER_BEHAVIOR": "effect_then_crash",
            "APLEXER_FAKE_DELIVER_LOG": log_file
        }):
            with self.assertRaises(BridgeCapabilityGap):
                bridge.forward_hub_to_native(hub_msg)

        native_id = bridge.receipts["hub_to_native"]["hub-reconcile-test"]

        # Recipient sends authentic ACK via fake CLI
        ack_proc = subprocess.run(
            [env["fake_bin"], "message", "ack", native_id],
            cwd=env["work"],
            capture_output=True,
            text=True,
            env={
                "APLEXER_STATE_DIR": env["state_dir"],
                "APLEXER_WORKSPACE": env["canonical_ws"],
                "APLEXER_SESSION_ID": env["recip_id"],
            },
        )
        self.assertEqual(ack_proc.returncode, 0)

        # Retry forward reconciles without running deliver again
        with patch.dict(os.environ, {
            "APLEXER_FAKE_DELIVER_BEHAVIOR": "effect_then_crash",
            "APLEXER_FAKE_DELIVER_LOG": log_file
        }):
            resolved_id = bridge.forward_hub_to_native(hub_msg)
            self.assertEqual(resolved_id, native_id)
            self.assertEqual(bridge.receipts["delivers"][native_id]["status"], "delivered")

        with open(log_file, "r", encoding="utf-8") as f:
            total_writes = f.readlines()
        self.assertEqual(len(total_writes), 1, "Terminal delivery reconciled with zero additional writes")

    def test_fake_native_b2_masked_or_absent_binding_refused(self):
        """Fake native send envelope without bridge own session binding is refused into ambiguous_sends."""
        td = get_test_tmp_dir()
        env = setup_authoritative_test_env(td)

        # Create bridge with NO service session ID or ambient binding
        bridge = MailboxBridge(
            env["work"],
            aplexer_bin=env["fake_bin"],
            state_dir=env["state_dir"],
            runtime_dir=env["runtime_dir"],
            service_session_id=None,
            service_tag="custom-unbound-service",
        )
        self.assertIsNone(bridge.service_binding)

        hub_msg = {
            "id": "hub-b2-masked",
            "to_actor": env["recip_tag"],
            "from_actor": "lead",
            "body": "unauthenticated test",
            "kind": "note",
        }

        with self.assertRaises(BridgeCapabilityGap) as ctx:
            bridge.forward_hub_to_native(hub_msg)

        self.assertIn("unqualified", str(ctx.exception).lower())
        self.assertNotIn("hub-b2-masked", bridge.receipts.get("hub_to_native", {}))
        self.assertIn("hub-b2-masked", bridge.receipts.get("ambiguous_sends", {}))


if __name__ == "__main__":
    unittest.main()
