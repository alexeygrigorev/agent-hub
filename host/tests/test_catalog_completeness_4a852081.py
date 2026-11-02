import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from agent_hub_host.catalog_completeness import census_is_complete
from agent_hub_host.fleet import CATALOG_LIMIT, Dispatcher


class CatalogCompletenessTests(unittest.TestCase):
    @staticmethod
    def confirmed_exit(row):
        return row.get('confirmed_exit') is True

    def test_small_catalog_is_complete_without_positive_exit_evidence(self):
        rows = [
            {'native_id': 'running', 'confirmed_exit': False},
            {'native_id': 'broken', 'confirmed_exit': False},
        ]

        self.assertTrue(census_is_complete(rows, rows, limit=2, confirmed_exit=self.confirmed_exit))
        self.assertFalse(census_is_complete(rows, rows[:1], limit=2, confirmed_exit=self.confirmed_exit))

    def test_omitted_running_worker_false_and_unconfirmed_terminal_are_ambiguous(self):
        selected = [
            {'native_id': 'history-1', 'confirmed_exit': True},
            {'native_id': 'history-2', 'confirmed_exit': True},
        ]
        all_rows = selected + [
            {'native_id': 'running-dead-worker', 'phase': 'running', 'worker_alive': False, 'confirmed_exit': False},
            {'native_id': 'terminal-label-only', 'phase': 'exited', 'confirmed_exit': False},
        ]

        self.assertFalse(census_is_complete(all_rows, selected, limit=2, confirmed_exit=self.confirmed_exit))

    def test_truncated_positive_terminal_history_can_remain_complete(self):
        selected = [
            {'native_id': 'running', 'confirmed_exit': False},
            {'native_id': 'broken', 'confirmed_exit': False},
        ]
        all_rows = selected + [
            {'native_id': 'exited-1', 'confirmed_exit': True},
            {'native_id': 'exited-2', 'confirmed_exit': True},
        ]

        self.assertTrue(census_is_complete(all_rows, selected, limit=2, confirmed_exit=self.confirmed_exit))

    def test_missing_exit_evidence_fails_closed_and_invalid_limit_is_rejected(self):
        all_rows = [
            {'native_id': 'running'},
            {'native_id': 'unconfirmed-terminal'},
        ]
        self.assertFalse(census_is_complete(all_rows, all_rows[:1], limit=1, confirmed_exit=self.confirmed_exit))
        with self.assertRaisesRegex(ValueError, 'limit must be a positive integer'):
            census_is_complete(all_rows, all_rows, limit=True, confirmed_exit=self.confirmed_exit)

    def test_duplicate_missing_and_unrelated_native_identities_fail_closed(self):
        duplicate = [
            {'native_id': 'same', 'confirmed_exit': True},
            {'id': 'same', 'confirmed_exit': True},
        ]
        self.assertFalse(census_is_complete(duplicate, duplicate[:1], limit=1, confirmed_exit=self.confirmed_exit))
        self.assertFalse(census_is_complete([{'phase': 'running'}], [], limit=1, confirmed_exit=self.confirmed_exit))
        self.assertFalse(census_is_complete(
            [{'native_id': 'observed'}], [{'native_id': 'foreign'}], limit=1, confirmed_exit=self.confirmed_exit))
        self.assertFalse(census_is_complete(
            [{'native_id': 'native-1', 'id': 'native-2'}], [], limit=1, confirmed_exit=self.confirmed_exit))
        self.assertFalse(census_is_complete(
            [{'native_id': ''}], [], limit=1, confirmed_exit=self.confirmed_exit))

    def test_501_ambiguous_rows_cannot_form_a_complete_500_row_census(self):
        all_rows = [
            {'id': f'native-{index}', 'confirmed_exit': False}
            for index in range(501)
        ]

        self.assertFalse(census_is_complete(all_rows, all_rows[:500], limit=500, confirmed_exit=self.confirmed_exit))

    def report_census(self, rows, *, confirmed_exit):
        dispatcher = Dispatcher.__new__(Dispatcher)
        dispatcher.settings = {'subscriptions': [], 'aplexer_bin': 'aplexer'}
        dispatcher._catalog_enrichment = {}
        dispatcher.runner = Mock(return_value={'sessions': rows})
        requests = []

        def request(method, path, payload=None):
            requests.append((method, path, payload))
            return {'sessions': []} if path == '/api/sessions/host-bindings' else {}

        dispatcher.request = request
        with patch('agent_hub_host.account_capacity.confirmed_empty', side_effect=confirmed_exit), \
                contextlib.redirect_stdout(io.StringIO()):
            dispatcher._report_native_bindings()
        return next(payload for method, path, payload in requests if path == '/api/fleet/sessions')

    def test_host_report_marks_omitted_ambiguous_native_incomplete(self):
        rows = [
            {'id': f'native-{index}', 'tag': f'worker-{index}', 'engine': 'codex', 'profile': None,
             'workspace': '/repo', 'state': 'running', 'phase': 'running', 'worker_alive': False,
             'created_at_ms': index + 1}
            for index in range(CATALOG_LIMIT + 1)
        ]

        payload = self.report_census(rows, confirmed_exit=lambda row: False)

        self.assertEqual(len(payload['sessions']), CATALOG_LIMIT)
        self.assertIs(payload['census_complete'], False)

    def test_host_report_allows_omitted_rows_only_with_positive_exit_proof(self):
        rows = [
            {'id': f'native-{index}', 'tag': f'worker-{index}', 'engine': 'codex', 'profile': None,
             'workspace': '/repo', 'state': 'running', 'phase': 'running', 'worker_alive': True,
             'created_at_ms': index + 1}
            for index in range(CATALOG_LIMIT)
        ]
        rows.append({'id': 'old-terminal', 'tag': 'old-terminal', 'engine': 'codex', 'profile': None,
                     'workspace': '/repo', 'state': 'exited', 'phase': 'exited', 'worker_alive': False,
                     'created_at_ms': 1})

        payload = self.report_census(rows, confirmed_exit=lambda row: row.get('id') == 'old-terminal')

        self.assertEqual(len(payload['sessions']), CATALOG_LIMIT)
        self.assertTrue(payload['census_complete'])


if __name__ == '__main__':
    unittest.main()
