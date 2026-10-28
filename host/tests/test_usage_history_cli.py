import argparse
import unittest
from unittest.mock import patch
from agent_hub_host import quota

class UsageHistoryCliTests(unittest.TestCase):
    def parse(self, *argv):
        parser=argparse.ArgumentParser()
        quota.add_commands(parser.add_subparsers(dest='command'))
        return parser.parse_args(['quota','history',*argv])

    def test_shared_store_range_and_kind(self):
        with patch('agent_hub_host.bus._do_request',return_value={'series':[]}) as request:
            quota.execute(self.parse('--range','30d','--kind','tokens','--host','host-1'))
        request.assert_called_once_with('GET','/api/monitoring/history?range=30d&kind=tokens&machine=host-1')

    def test_rejects_ignored_filters_and_mutation(self):
        for argv in [('--provider','codex'),('--engine','codex'),('--account','shared-codex'),('--reason','write')]:
            with self.assertRaises(ValueError): quota.execute(self.parse(*argv))

    def test_text_preserves_unknown_and_known_zero(self):
        text=quota.render_text({'series':[{'kind':'quota_window','entity':'account','metric':'5h','points':[{'value':None},{'value':0}]}]})
        self.assertIn('1 known points',text)
