import argparse
import unittest
from unittest import mock

from agent_hub_host import releases

SLOT_A = '00000000-0000-4000-8000-000000000001'


def parse(*argv):
    parser = argparse.ArgumentParser()
    releases.add_parser(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(['releases', *argv])


class ReleasesCliTests(unittest.TestCase):
    """`hub releases` argument mapping; HTTP stays behind session_control."""

    def test_issue_sha_maps_to_post_slots(self):
        with mock.patch('agent_hub_host.session_control.request', return_value={}) as req:
            releases.execute(parse('issue', '--sha', 'A' * 40, '--id', SLOT_A,
                                   '--ttl-hours', '24', '--note', 'bootstrap'))
            req.assert_called_once_with('POST', '/api/releases/slots', {
                'sha': 'a' * 40, 'id': SLOT_A, 'ttl_hours': 24.0, 'note': 'bootstrap'})

    def test_issue_version_target_for_rollback(self):
        with mock.patch('agent_hub_host.session_control.request', return_value={}) as req:
            target = 'cf8aef89-43de-4732-a2aa-e34ce91a426d'
            releases.execute(parse('issue', '--target', target))
            req.assert_called_once_with('POST', '/api/releases/slots', {'target': target})

    def test_show_list_revoke_use_bounded_reads(self):
        with mock.patch('agent_hub_host.session_control.request', return_value={'slots': []}) as req:
            releases.execute(parse('show', SLOT_A))
            req.assert_called_with('GET', '/api/releases/slots/' + SLOT_A)
            releases.execute(parse('list', '--limit', '3', '--status', 'issued'))
            req.assert_called_with('GET', '/api/releases/slots?limit=3&status=issued')
            releases.execute(parse('revoke', SLOT_A))
            req.assert_called_with('POST', '/api/releases/slots/' + SLOT_A + '/revoke', {})

    def test_invalid_input_never_reaches_http(self):
        cases = [
            ('issue',),
            ('issue', '--sha', 'short'),
            ('issue', '--sha', 'a' * 40),
            ('issue', '--target', 'a' * 40),
            ('issue', '--sha', 'a' * 40, '--target', 'version:x'),
            ('issue', '--sha', 'a' * 40, '--id', 'guessable'),
            ('issue', '--sha', 'a' * 40, '--ttl-hours', '0'),
            ('issue', '--sha', 'a' * 40, '--ttl-hours', '337'),
            ('list', '--limit', '201'),
            ('list', '--limit', '0'),
        ]
        for argv in cases:
            with self.subTest(argv=argv), mock.patch('agent_hub_host.session_control.request') as req:
                with self.assertRaises(ValueError):
                    releases.execute(parse(*argv))
                req.assert_not_called()

    def test_render_lists_slots_and_single_records(self):
        shown = {'slots': [{'id': SLOT_A, 'sha': 'a' * 40, 'target': 'a' * 40, 'status': 'issued',
                            'issued_by': 'orchestrator-main', 'expires_at': 123, 'consumer': None, 'note': 'bootstrap deploy'}]}
        text = releases.render(parse('list'), shown)
        self.assertIn(SLOT_A, text)
        self.assertIn('issued', text)
        single = releases.render(parse('issue', '--sha', 'a' * 40),
                                 {'id': SLOT_A, 'target': 'a' * 40, 'sha': 'a' * 40, 'status': 'issued',
                                  'issued_by': 'orchestrator-main', 'expires_at': 123, 'consumer': None, 'note': None})
        self.assertIn('issued', single)
        self.assertIn('orchestrator-main', single)


if __name__ == '__main__':
    unittest.main()
