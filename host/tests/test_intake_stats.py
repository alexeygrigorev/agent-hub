import argparse
import unittest
from unittest.mock import patch
from agent_hub_host import intake, intake_stats

class IntakeStatsTests(unittest.TestCase):
    def parser(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest='cmd', required=True)
        intake.add_commands(sub)
        intake_stats.add_commands(sub)
        return p

    def test_individual_json_uses_authorized_endpoint(self):
        args = self.parser().parse_args(['intake', 'stats', 'abcdef', '--json'])
        with patch.object(intake, 'request', return_value={'stats': {}}) as request:
            self.assertEqual(intake_stats.execute(args), {'stats': {}})
            request.assert_called_once_with('GET', '/api/intake/tasks/abcdef/stats')

    def test_global_options_are_encoded(self):
        args = self.parser().parse_args(['stats', '--window', '7d', '--project', 'a&b', '--sort', 'lead_time_ms', '--order', 'asc', '--json'])
        with patch.object(intake, 'request', return_value={}) as request:
            intake_stats.execute(args)
            self.assertIn('project=a%26b', request.call_args.args[1])
            self.assertIn('window=7d', request.call_args.args[1])

    def test_invalid_window_rejected(self):
        with self.assertRaises(SystemExit):
            self.parser().parse_args(['stats', '--window', 'forever'])
