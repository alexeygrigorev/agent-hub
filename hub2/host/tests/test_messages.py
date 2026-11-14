import json
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path
import test_host

from agent_hub_host.client import ApiError
from agent_hub_host.messages import deliver


class MailTests(unittest.TestCase):
    # Host fixture helpers; individual delivery assertions use real journal transactions.
    setUp = test_host.HostTests.setUp
    tearDown = test_host.HostTests.tearDown
    offer = test_host.HostTests.offer
    def message(self):
        return {"id": "mail-1", "epoch": 1, "recipient": "session:session-1", "session_id": "session-1",
                "operation_id": "delivery-op-1", "body": "Private synthetic inbox text"}

    def test_native_mail_effect_once_and_receipt_replayed_after_network_loss(self):
        self.bridge.offer(self.offer())
        self.journal.flush(self.client)
        calls = []
        real_request = self.client.request
        lost = [False]
        def request(method, path, body=None, key=None):
            if body and body.get("phase") == "claim":
                return {"claim_id": "claim-1", "epoch": 1}
            if body and body.get("phase") == "receipt" and not lost[0]:
                lost[0] = True
                raise ApiError("transport unavailable")
            return real_request(method, path, body, key)
        self.client.request = request
        def native(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0, "11bdb3e2-854e-4aaa-9ddd-3e3ec823f2c4\n", "")
        with self.assertRaises(ApiError):
            deliver(self.bridge, self.message(), native)
        receipt = deliver(self.bridge, self.message(), native)
        self.journal.flush(self.client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(receipt["message_id"], "11bdb3e2-854e-4aaa-9ddd-3e3ec823f2c4")
        self.assertEqual(self.client.calls[-1][2]["native_receipt"], receipt)
        self.assertNotIn("APLEXER_SESSION_ID", calls[0][1]["env"])
        self.assertTrue(Path(calls[0][1]["cwd"]).samefile(self.root))
        self.assertNotIn("--from", calls[0][0])

    def test_unconfirmed_effect_remains_uncertain_and_is_not_retried(self):
        self.bridge.offer(self.offer())
        self.client.request = lambda *args: {"claim_id": "claim-1", "epoch": 1}
        effects = []
        def native(arguments, **options):
            effects.append(arguments)
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            deliver(self.bridge, self.message(), native)
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            deliver(self.bridge, self.message(), native)
        self.assertEqual(len(effects), 1)
        row = self.journal.db.execute("SELECT state,receipt FROM native_mail").fetchone()
        self.assertEqual(row["state"], "executing")
        self.assertIsNone(row["receipt"])


    def test_mailbox_uncertainty_reports_message_epoch_and_exact_durable_native(self):
        self.bridge.offer(self.offer())
        launch = self.journal.db.execute("SELECT * FROM launches").fetchone()
        message = self.message(); message['epoch'] = launch['epoch'] + 7
        message['native_id'] = 'caller-forged-native'
        frames = []
        class Socket:
            def send(self, raw): frames.append(json.loads(raw))
        with patch('agent_hub_host.messages.deliver', side_effect=RuntimeError('native_mail_outcome_uncertain')):
            self.bridge.dispatch({'type': 'mailbox.deliver', 'message': message}, Socket(), None)
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame['reason'], 'native_mail_outcome_uncertain')
        self.assertEqual(frame['stage'], 'host_mailbox')
        self.assertEqual(frame['message_epoch'], message['epoch'])
        self.assertNotEqual(frame['message_epoch'], launch['epoch'])
        self.assertEqual(frame['native_id'], launch['native_id'])
        self.assertNotEqual(frame['native_id'], message['native_id'])
        self.assertNotIn('epoch', frame)
        self.assertEqual((frame['message_id'], frame['operation_id'], frame['session_id']),
                         (message['id'], message['operation_id'], message['session_id']))


    def test_mailbox_fatal_boundary_still_propagates_without_error_frame(self):
        frames = []
        class Socket:
            def send(self, raw): frames.append(raw)
        error = KeyboardInterrupt()
        with patch('agent_hub_host.messages.deliver', side_effect=error):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.bridge.dispatch({'type': 'mailbox.deliver', 'message': self.message()}, Socket(), None)
        self.assertIs(caught.exception, error)
        self.assertEqual(frames, [])


if __name__ == "__main__":
    unittest.main()
