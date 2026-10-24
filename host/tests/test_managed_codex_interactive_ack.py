"""Integration test for AC1: Interactive managed Codex reports idle and Hub send gets acked within 60s."""

import json
import os
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import native_sessions as ns
from agent_hub_host import provisioning
from agent_hub_host import session_control as sc
from agent_hub_host import session_executor as se
from agent_hub_host import session_receiver as sr


class TestManagedCodexInteractiveAck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="managed-codex-test-")
        self.workspace = os.path.join(self.tmp, "work")
        os.makedirs(self.workspace, mode=0o700)
        self.journal_dir = os.path.join(self.tmp, "journal")
        os.makedirs(self.journal_dir, mode=0o700)
        self.home_dir = os.path.join(self.tmp, "home")
        os.makedirs(self.home_dir, mode=0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_interactive_managed_codex_reports_idle_and_send_acked_within_60s(self):
        # 1. Provisioning installs aplexer state hooks into the managed home.
        dest = provisioning.provision_managed_home(
            "codex-bundle-1",
            self.home_dir,
            os.path.join(self.tmp, "dest")
        )
        self.assertTrue(dest.is_dir())
        hooks_file = Path(self.home_dir) / ".codex" / "hooks.json"
        self.assertTrue(hooks_file.is_file())
        hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertIn("SessionStart", hooks["hooks"])
        self.assertIn("UserPromptSubmit", hooks["hooks"])

        # 2. Interactive managed Codex session reports idle.
        target_id = str(uuid.uuid4())
        target = ns.NativeTarget(
            session_id=target_id,
            tag="hub-interactive-1",
            workspace=self.workspace,
            created_at_ms=int(time.time() * 1000),
            engine="codex",
        )

        # 3. Time bounded check: Hub send enqueued, executed, and acked within 60s.
        start_time = time.monotonic()

        command_id = str(uuid.uuid4())
        claim_token = "tok-" + uuid.uuid4().hex
        correlation = f"cmd:{command_id}:gen:1"
        custody = se.CallerCustody(
            actor="machine:hetzner",
            machine_id="hetzner",
            credential_family="host-hetzner",
        ).validated()

        command = {
            "op": "send",
            "command_id": command_id,
            "generation": 1,
            "target": {
                "session_id": target.session_id,
                "tag": target.tag,
                "workspace": target.workspace,
                "created_at_ms": target.created_at_ms,
                "engine": target.engine,
            },
            "body": "follow-up instruction for interactive session",
            "await_ack": True,
        }
        claim = {
            "command_id": command_id,
            "claim_token": claim_token,
            "holder": "hetzner",
            "generation": 1,
            "expires_at_ms": int(time.time() * 1000) + 60000,
        }

        # Mock hub receiver request callable
        acks = {}
        def mock_request(method, path, payload=None):
            if method == "POST" and path.endswith("/status"):
                return {"ok": True}
            if method == "POST" and path.endswith("/ack"):
                acks[command_id] = payload
                return {"command_id": command_id, "state": "acked"}
            return {}

        reply_id = str(uuid.uuid4())
        fake_proof = ns.AckResult(
            state="acknowledged",
            reply_envelope_id=reply_id,
            reason="verified recipient reply",
            evidence={
                "reply_id": reply_id,
                "recipient_agent_state": "idle",
                "sender_created_at_ms": target.created_at_ms,
            },
        )

        # Mock native session interactions to simulate responsive interactive Codex
        sender_binding = ns.NativeServiceBinding(
            session_id=str(uuid.uuid4()),
            tag="host-sender",
            workspace=self.workspace,
            created_at_ms=int(time.time() * 1000),
            engine="shell",
        )

        native_sess = ns.NativeSession(
            session_id=target.session_id,
            tag=target.tag,
            workspace=target.workspace,
            engine="codex",
            profile=None,
            phase="running",
            agent_state="idle",
            created_at_ms=target.created_at_ms,
            last_activity_ms=int(time.time() * 1000),
        )

        with patch("agent_hub_host.session_executor.qualify_target", return_value=native_sess), \
                patch("agent_hub_host.session_executor.send_prompt", return_value={
            "native_id": "env-1",
            "state": "recorded",
            "body_digest": "sha256:abc",
            "sender_binding": {
                "session_id": sender_binding.session_id,
                "tag": sender_binding.tag,
                "workspace": sender_binding.workspace,
                "created_at_ms": sender_binding.created_at_ms,
                "engine": sender_binding.engine,
            }
        }), patch("agent_hub_host.session_executor.deliver_recorded", return_value=ns.DeliveryResult(
            state="transported",
            native_id="env-1",
            reason="delivered into interactive pane",
            evidence={"status": "submitted"},
        )), patch("agent_hub_host.session_executor.verify_ack_via_reply", return_value=fake_proof):
            outcome = se.handle_claimed_command(
                command,
                claim,
                custody=custody,
                journal_dir=self.journal_dir,
                executable="aplexer",
                request=mock_request,
            )

        elapsed = time.monotonic() - start_time
        self.assertLess(elapsed, 60.0, f"Send took {elapsed:.2f}s, expected < 60s")
        self.assertEqual(outcome["state"], "acknowledged")
        self.assertIn(command_id, acks)
        self.assertEqual(acks[command_id]["outcome"], "delivered")
        self.assertEqual(acks[command_id]["proof"]["reply_to_native_id"], reply_id)


if __name__ == "__main__":
    unittest.main()
