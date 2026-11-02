"""`hub sessions list` HOST/ACCOUNT columns and `hub sessions count --by` (intake 93e7817b).

Synthetic transport only: never contacts a Hub.
"""
import argparse
import unittest
from unittest.mock import patch

from agent_hub_host import session_control
from agent_hub_host import limits_cli


def parse(*args):
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root.parse_args(['sessions', *args])


BREAKDOWN = {
    'view': 'breakdown', 'by': ['host', 'engine', 'account'],
    'totals': {'running': 3, 'queued': 1, 'stalled': 1, 'total': 5},
    'rows': [
        {'host': 'DEFLf4', 'host_label': 'urgent-hetzner', 'host_aliases': [{'id': 'SnV2sx', 'label': 'hetzner'}],
         'engine': 'codex', 'account': 'shared-codex', 'running': 2, 'queued': 0, 'stalled': 1, 'total': 3},
        {'host': 'win', 'host_label': 'DESKTOP-FP4OP26', 'host_aliases': [],
         'engine': 'claude', 'account': 'claude-max', 'running': 1, 'queued': 1, 'stalled': 0, 'total': 2},
    ],
    'scanned': 5, 'truncated': False,
}


class TestSessionsList(unittest.TestCase):
    def test_list_table_has_host_and_account_columns(self):
        table = session_control.render_table([
            {'id': 'a', 'name': 'w1', 'machine': 'urgent-hetzner', 'engine': 'codex', 'account': 'shared-codex', 'state': 'running'},
            {'id': 'b', 'name': 'w2', 'machine': None, 'engine': 'claude', 'account': None, 'state': 'held'},
        ])
        header = table.splitlines()[0].split()
        self.assertEqual(header[:5], ['TYPE', 'NAME', 'HOST', 'ENGINE', 'ACCOUNT'])
        first = table.splitlines()[2].split()
        self.assertEqual(first[2:5], ['urgent-hetzner', 'codex', 'shared-codex'])
        second = table.splitlines()[3].split()
        self.assertEqual(second[2:5], ['-', 'claude', '-'])


class TestSessionsCount(unittest.TestCase):
    def test_default_groups_by_host_engine_account(self):
        with patch.object(session_control, 'request', return_value=BREAKDOWN) as transport:
            result = session_control.execute(parse('count'))
        transport.assert_called_once_with('GET', '/api/sessions?view=breakdown&by=host,engine,account')
        self.assertEqual(result['totals']['total'], 5)

    def test_by_host_only_and_dedupes(self):
        with patch.object(session_control, 'request', return_value=BREAKDOWN) as transport:
            session_control.execute(parse('count', '--by', 'Host, host'))
        transport.assert_called_once_with('GET', '/api/sessions?view=breakdown&by=host')

    def test_unknown_dimension_is_refused_before_any_request(self):
        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(session_control.SessionInputError):
                session_control.execute(parse('count', '--by', 'host,model'))
        transport.assert_not_called()

    def test_text_render_shows_counts_aliases_and_totals(self):
        text = session_control.render_output(parse('count'), BREAKDOWN)
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ['HOST', 'ENGINE', 'ACCOUNT', 'RUNNING', 'QUEUED', 'STALLED', 'TOTAL'])
        self.assertIn('urgent-hetzner (aka hetzner)', lines[2])
        self.assertEqual(lines[2].split()[-4:], ['2', '0', '1', '3'])
        self.assertEqual(lines[-1].split(), ['TOTAL', '3', '1', '1', '5'])

    def test_json_flag_returns_raw_payload(self):
        self.assertIs(session_control.render_output(parse('count', '--json'), BREAKDOWN), BREAKDOWN)

    def test_partial_grouping_render(self):
        payload = {'by': ['account'], 'totals': {'running': 8, 'queued': 0, 'stalled': 0, 'total': 8},
                   'rows': [{'host': None, 'engine': None, 'account': 'shared-codex', 'running': 8, 'queued': 0, 'stalled': 0, 'total': 8}]}
        lines = session_control.render_breakdown(payload).splitlines()
        self.assertEqual(lines[0].split(), ['ACCOUNT', 'RUNNING', 'QUEUED', 'STALLED', 'TOTAL'])
        self.assertEqual(lines[2].split(), ['shared-codex', '8', '0', '0', '8'])


class TestLimitsShowSessions(unittest.TestCase):
    def test_limits_show_prints_live_session_count_next_to_active(self):
        args = argparse.Namespace(json=False, limits_cmd='show')
        text = limits_cli.render(args, {'engines': [], 'accounts': [
            {'account': 'shared-codex', 'provider': 'codex', 'max_concurrency': 30, 'active': 23, 'reserved': 5,
             'observed_sessions': 18, 'native_live': 8, 'sessions': 8, 'available_slots': 7, 'enabled': True, 'status': 'ok'}]})
        lines = text.splitlines()
        header = next(line for line in lines if line.startswith('ACCOUNT')).split()
        row = next(line for line in lines if line.startswith('shared-codex')).split()
        self.assertEqual(row[header.index('SESSIONS')], '8')
        self.assertEqual(row[header.index('ACTIVE')], '23')


if __name__ == '__main__':
    unittest.main()
