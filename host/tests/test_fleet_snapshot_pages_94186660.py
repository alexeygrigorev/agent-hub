"""Intake 94186660: `hub fleet` walks the keyset-paged sessions list."""
import unittest
from unittest.mock import patch

from agent_hub_host import session_control


class FetchFleetSnapshotTests(unittest.TestCase):
    def test_walks_pages_until_cursor_null(self):
        pages = [
            {'sessions': [{'id': 1}], 'sessions_cursor_next': 'c1', 'machines': []},
            {'sessions': [{'id': 2}], 'sessions_cursor_next': 'c2'},
            {'sessions': [{'id': 3}], 'sessions_cursor_next': None},
        ]
        with patch.object(session_control, 'request', side_effect=list(pages)) as req:
            out = session_control.fetch_fleet_snapshot(limit=500)
        self.assertEqual([s['id'] for s in out['sessions']], [1, 2, 3])
        self.assertNotIn('sessions_cursor_next', out)
        self.assertEqual(req.call_count, 3)
        self.assertIn('sessions_limit=500', req.call_args_list[0].args[1])
        self.assertIn('sessions_cursor=c2', req.call_args_list[2].args[1])

    def test_old_server_without_cursor_is_one_fetch(self):
        with patch.object(session_control, 'request',
                          return_value={'sessions': [{'id': 1}], 'machines': []}) as req:
            out = session_control.fetch_fleet_snapshot()
        self.assertEqual(out['sessions'], [{'id': 1}])
        self.assertEqual(req.call_count, 1)

    def test_repeated_cursor_terminates(self):
        loop = {'sessions': [], 'sessions_cursor_next': 'stuck'}
        with patch.object(session_control, 'request', return_value=dict(loop)) as req:
            out = session_control.fetch_fleet_snapshot(max_pages=20)
        self.assertEqual(out['sessions'], [])
        self.assertLessEqual(req.call_count, 3)

    def test_non_dict_passthrough(self):
        with patch.object(session_control, 'request', return_value=[1, 2]):
            self.assertEqual(session_control.fetch_fleet_snapshot(), [1, 2])


if __name__ == '__main__':
    unittest.main()
