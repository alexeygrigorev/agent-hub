import copy
import unittest
from unittest.mock import Mock

from agent_hub_host.bus import HttpRequestError
from agent_hub_host.catalog_transport import report_catalog


class CatalogTransportCompletenessTests(unittest.TestCase):
    def test_explicit_incomplete_census_survives_legacy_progress_retry(self):
        rows = [{
            'native_id': 'native-1',
            'phase': 'running',
            'progress': {
                'last_progress_at': '2026-11-01T08:00:00Z',
                'sampled_at': '2026-11-01T08:01:00Z',
                'goal_status': 'active',
            },
        }]
        original = copy.deepcopy(rows)
        request = Mock(side_effect=[
            HttpRequestError(400, '{"error":"invalid_session"}'),
            {'ok': True},
        ])

        result = report_catalog(request, rows, census_complete=False)

        self.assertEqual(result, {'ok': True})
        self.assertEqual(request.call_args_list[0].args[2], {
            'sessions': rows,
            'census_complete': False,
        })
        self.assertEqual(request.call_args_list[1].args[2], {
            'sessions': [{
                'native_id': 'native-1',
                'phase': 'running',
                'progress': {'last_progress_at': '2026-11-01T08:00:00Z'},
            }],
            'census_complete': False,
        })
        self.assertEqual(rows, original)

    def test_explicit_complete_census_is_sent_unchanged(self):
        rows = [{'native_id': 'native-1', 'phase': 'running'}]
        request = Mock(return_value={'ok': True})

        self.assertEqual(report_catalog(request, rows, census_complete=True), {'ok': True})

        request.assert_called_once_with('PUT', '/api/fleet/sessions', {
            'sessions': rows,
            'census_complete': True,
        })

    def test_rejects_non_boolean_census_completeness_before_request(self):
        request = Mock()

        with self.assertRaisesRegex(TypeError, 'census_complete must be a boolean'):
            report_catalog(request, [], census_complete=1)

        request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
