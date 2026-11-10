"""Permanent receipt refusals must not prevent host socket reconnection."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_hub_host.agent import Journal
from agent_hub_host.client import ApiError


class OutboxDeadletterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / 'journal.sqlite')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.journal.db.close)

    def test_rejected_finish_is_preserved_and_later_receipts_are_delivered(self):
        payload = {'native_id': 'native', 'evidence': {'marker': 'private-sentinel'}}
        self.journal.emit('finish:old', '/api/sessions/old/finish', payload)
        self.journal.emit('started:new', '/api/sessions/new/started', {'native_id': 'new'})
        client = Mock()
        client.request.side_effect = [ApiError('refused', status=409, code='unqualified_native_exit_evidence'), {}]
        with self.assertLogs('agent_hub_host.agent', level='ERROR') as logs:
            self.journal.flush(client)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        row = self.journal.db.execute('SELECT * FROM outbox_dead_letters').fetchone()
        self.assertEqual(json.loads(row['payload']), payload)
        self.assertEqual(row['status'], 409)
        self.assertEqual(row['reason'], 'unqualified_native_exit_evidence')
        self.assertGreater(row['observed_at'], 0)
        self.assertNotIn('private-sentinel', '\n'.join(logs.output))
        self.journal.flush(client)
        self.assertEqual(client.request.call_count, 2)

    def test_transient_auth_and_budget_failures_remain_retryable(self):
        failures = [ApiError('transient', status=status) for status in (None, 401, 403, 429, 500, 503, 504)]
        failures.append(ApiError('budget pause', status=409, retry_at=123))
        for index, failure in enumerate(failures):
            with self.subTest(status=failure.status, retry_at=failure.retry_at):
                self.journal.db.execute('DELETE FROM outbox')
                self.journal.db.commit()
                self.journal.emit(str(index), '/api/sessions/live/finish', {'receipt': index})
                with self.assertRaises(ApiError):
                    self.journal.flush(Mock(request=Mock(side_effect=failure)))
                self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
                self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox_dead_letters').fetchone()[0], 0)

    def test_permanent_refusals_have_private_deadletter_receipts(self):
        for status in (400, 404, 409, 410, 422):
            with self.subTest(status=status):
                self.journal.emit(str(status), '/api/sessions/old/finish', {'status': status})
                with self.assertLogs('agent_hub_host.agent', level='ERROR'):
                    self.journal.flush(Mock(request=Mock(side_effect=ApiError('refused', status=status))))
                row = self.journal.db.execute('SELECT * FROM outbox_dead_letters WHERE key=?', (str(status),)).fetchone()
                self.assertEqual(row['reason'], 'http_' + str(status))
                self.assertEqual(json.loads(row['payload']), {'status': status})

    def test_safety_flush_does_not_discard_unrelated_queue_entries(self):
        self.journal.emit('other', '/api/other', {'keep': True})
        self.journal.emit('finish', '/api/sessions/old/finish', {'terminal': True})
        client = Mock(request=Mock(side_effect=ApiError('retired fence', status=409)))
        with self.assertLogs('agent_hub_host.agent', level='ERROR'):
            self.journal.flush(client, safety_only=True)
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(self.journal.db.execute('SELECT key FROM outbox').fetchone()[0], 'other')
