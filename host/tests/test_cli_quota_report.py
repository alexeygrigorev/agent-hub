"""GrokBot <1% quota fallback CLI: `hub quota report|check <account>` (intake 408b585e).

Offline unit tests: argv mapping and endpoint/body contract via bus mock.
"""
import argparse
import json
import unittest
from unittest import mock

from agent_hub_host import cli
from agent_hub_host import quota as quota_cli


def parse(argv):
    parser = argparse.ArgumentParser()
    quota_cli.add_commands(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(argv)


class QuotaReportCliTest(unittest.TestCase):
    def test_report_posts_bounded_evidence_with_defaults(self):
        with mock.patch.object(quota_cli, 'time') as clock, \
                mock.patch('agent_hub_host.bus._do_request', return_value={'recorded': True}) as request:
            clock.time.return_value = 1000.5
            quota_cli.execute(parse(['quota', 'report', 'grokbot', '--remaining', '0.7',
                                     '--source', 'grok-replica', '--reset-at', '12345']))
            request.assert_called_once_with('PUT', '/api/quota/grokbot', {
                'remaining': 0.7, 'source': 'grok-replica', 'captured_at': 1000500, 'reset_at': 12345})

    def test_report_accepts_threshold_and_freshness_window(self):
        with mock.patch('agent_hub_host.bus._do_request', return_value={'recorded': True}) as request:
            quota_cli.execute(parse(['quota', 'report', 'grokbot', '--remaining', '20', '--source', 'probe',
                                     '--threshold', '1', '--valid-for-ms', '600000', '--captured-at', '5']))
            request.assert_called_once_with('PUT', '/api/quota/grokbot', {
                'remaining': 20.0, 'source': 'probe', 'captured_at': 5,
                'valid_for_ms': 600000, 'threshold': 1.0})

    def test_report_requires_remaining_and_source_and_validates_values(self):
        with mock.patch('agent_hub_host.bus._do_request') as request:
            with self.assertRaises(ValueError):
                quota_cli.execute(parse(['quota', 'report', 'grokbot']))
            with self.assertRaises(ValueError):
                quota_cli.execute(parse(['quota', 'report', 'grokbot', '--remaining', '1']))
            for bad in ('-1', '101', 'abc'):
                with self.assertRaises(ValueError):
                    quota_cli.execute(parse(['quota', 'report', 'grokbot', '--remaining', bad,
                                             '--source', 'probe']))
            with self.assertRaises(ValueError):
                quota_cli.execute(parse(['quota', 'report', 'grokbot', '--remaining', '1',
                                         '--source', 'probe', '--valid-for-ms', '0']))
            request.assert_not_called()

    def test_check_reads_the_export_without_evidence_flags(self):
        payload = {'account': 'grokbot', 'below_threshold': True}
        with mock.patch('agent_hub_host.bus._do_request', return_value=payload) as request:
            self.assertEqual(quota_cli.execute(parse(['quota', 'check', 'grokbot'])), payload)
            request.assert_called_once_with('GET', '/api/quota/grokbot')
            with self.assertRaises(ValueError):
                quota_cli.execute(parse(['quota', 'check', 'grokbot', '--remaining', '1']))
            with self.assertRaises(ValueError):
                quota_cli.execute(parse(['quota', 'check']))

    def test_account_labels_are_bounded(self):
        with mock.patch('agent_hub_host.bus._do_request') as request, self.assertRaises(ValueError):
            quota_cli.execute(parse(['quota', 'check', 'Bad_Label']))
        with mock.patch('agent_hub_host.bus._do_request') as request:
            with self.assertRaises(ValueError):
                quota_cli.check_quota('missing-account/..')
            with self.assertRaises(ValueError):
                quota_cli.execute(parse(['quota', 'check', 'UPPER']))
        request.assert_not_called()

    def test_text_render_for_the_export_shape(self):
        line = quota_cli.render_text({'account': 'grokbot', 'state': 'exhausted', 'remaining_percent': 0.7,
                                      'threshold_percent': 1, 'below_threshold': True, 'fresh': True,
                                      'observed_at': '2026-11-19T00:00:00Z'})
        self.assertIn('grokbot', line)
        self.assertIn('below_threshold=True', line)
        self.assertIn('0.7%', line)

    def test_root_quota_command_dispatch(self):
        output = []
        with mock.patch.object(cli.sys, 'argv', ['hub', 'quota', 'check', 'grokbot', '--json']), \
                mock.patch.object(quota_cli, 'execute', return_value={'account': 'grokbot'}) as execute, \
                mock.patch('builtins.print', side_effect=lambda value, *a, **k: output.append(value)):
            cli.main()
        self.assertEqual(json.loads(output[-1]), {'account': 'grokbot'})
        self.assertEqual(execute.call_args.args[0].kill_action, 'grokbot')


if __name__ == '__main__':
    unittest.main()
