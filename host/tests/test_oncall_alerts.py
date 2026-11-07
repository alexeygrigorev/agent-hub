import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import oncall_alerts, private_fs


class OncallAlertsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = self.root / "journal"
        self.journal.mkdir(mode=0o700)
        self.token = "poll-token-" + "x" * 40
        private_fs.atomic_write(
            self.journal / oncall_alerts.CONFIG_NAME,
            json.dumps({"poll_token": self.token}).encode(),
            oncall_alerts.CONFIG_LIMIT,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_poll_saves_to_both_helpers_before_ack(self):
        delivery_id = "12345678-1234-1234-1234-123456789abc"
        calls = []

        def request(method, path, payload, token):
            calls.append((method, path, payload, token))
            if method == "GET":
                return {"offers": [{
                    "delivery_id": delivery_id,
                    "lease_token": "lease-token-" + "y" * 40,
                    "payload": {
                        "from": "alerts@example.invalid",
                        "to": "dev@dtcdev.click",
                        "subject": "[TEST] alert",
                        "text": "A test alert",
                    },
                }]}
            return {"saved": True}

        helpers = []

        def helper_runner(role, helper, body):
            helpers.append((role, helper, body))
            return True

        result = oncall_alerts.poll_once(
            str(self.journal), request=request, helper_runner=helper_runner,
        )
        self.assertEqual(result, {"disabled": False, "offers": 1, "saved": 1, "errors": 0})
        self.assertEqual([(role, helper) for role, helper, _ in helpers], [
            ("oncall", "oncall-send"), ("orchestrator", "orch-send"),
        ])
        self.assertTrue(all(f"[hub-oncall-alert:{delivery_id}]" in body for _, _, body in helpers))
        self.assertEqual([method for method, *_ in calls], ["GET", "POST"])
        self.assertTrue(calls[-1][1].endswith("/ack"))
        self.assertEqual(calls[-1][3], self.token)

    def test_helper_exit_two_means_saved_inbox_even_when_session_is_down(self):
        home = self.root / "home"
        helper_dir = home / "bin"
        helper_dir.mkdir(parents=True)
        inbox = home / "tmp/agent-reports/oncall/inbox"
        body = "Dapier alert\n[hub-oncall-alert:12345678-1234-1234-1234-123456789abc]\nmessage\n"
        helper = helper_dir / "oncall-send"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import sys\n"
            "body = sys.stdin.read()\n"
            "inbox = Path.home() / 'tmp/agent-reports/oncall/inbox'\n"
            "inbox.mkdir(parents=True, exist_ok=True)\n"
            "path = inbox / 'saved.md'\n"
            "path.write_text(body)\n"
            "print(f'INBOX=saved {path}')\n"
            "print('DELIVERY=not-delivered (no running oncall-engineer session)')\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        helper.chmod(0o700)
        with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"HOME": str(home)}):
            self.assertTrue(oncall_alerts._run_helper("oncall", "oncall-send", body))
            saved = inbox / "saved.md"
            self.assertTrue(saved.is_file())
            self.assertIn("[hub-oncall-alert:12345678-1234-1234-1234-123456789abc]", saved.read_text())

            def should_not_run(*_args, **_kwargs):
                self.fail("saved delivery should be deduplicated on retry")

            with patch.object(oncall_alerts.subprocess, "run", side_effect=should_not_run):
                self.assertTrue(oncall_alerts._run_helper("oncall", "oncall-send", body))

    def test_failed_inbox_save_nacks_but_still_attempts_orchestrator_helper(self):
        calls = []

        def request(method, path, payload, token):
            calls.append((method, path))
            if method == "GET":
                return {"offers": [{
                    "delivery_id": "12345678-1234-1234-1234-123456789abc",
                    "lease_token": "lease-token-" + "z" * 40,
                    "payload": {"subject": "failed helper test", "text": "body"},
                }]}
            return {"retry": True}

        attempted = []

        def helper_runner(role, helper, _body):
            attempted.append((role, helper))
            return role == "orchestrator"

        result = oncall_alerts.poll_once(
            str(self.journal), request=request, helper_runner=helper_runner,
        )
        self.assertEqual(result["saved"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(attempted, [("oncall", "oncall-send"), ("orchestrator", "orch-send")])
        self.assertEqual(calls[-1][1], "/api/oncall-alerts/12345678-1234-1234-1234-123456789abc/nack")


if __name__ == "__main__":
    unittest.main()
