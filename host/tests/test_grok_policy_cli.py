import argparse
import unittest
from unittest.mock import patch
from agent_hub_host import routing


class GrokPolicyCliTests(unittest.TestCase):
    def parse(self, *argv):
        parser = argparse.ArgumentParser()
        routing.add_commands(parser.add_subparsers(dest='cmd'))
        return parser.parse_args(argv)

    @patch.object(routing.bus, '_do_request')
    def test_read_and_set(self, request):
        routing.execute(self.parse('model-policy', 'grok', '--json'))
        request.assert_called_with('GET', '/api/fleet/routing/policy/grok')
        routing.execute(self.parse('model-policy', 'grok', 'set', 'disabled', '--reason', 'bounded proof'))
        request.assert_called_with('POST', '/api/fleet/routing/policy/grok', {'mode': 'disabled', 'reason': 'bounded proof'})

    @patch.object(routing.bus, '_do_request')
    def test_each_preference_uses_the_preference_endpoint_only(self, request):
        for mode in ('disabled', 'minimal', 'normal'):
            with self.subTest(mode=mode):
                routing.execute(self.parse('model-policy', 'grok', 'set', mode, '--reason', 'saved preference only'))
                request.assert_called_with('POST', '/api/fleet/routing/policy/grok', {'mode': mode, 'reason': 'saved preference only'})

    @patch.object(routing.bus, '_do_request')
    def test_missing_reason_never_mutates(self, request):
        with self.assertRaises(ValueError):
            routing.execute(self.parse('model-policy', 'grok', 'set', 'normal'))
        request.assert_not_called()

    @patch.object(routing.bus, '_do_request')
    def test_legacy_read(self, request):
        routing.execute(self.parse('model-policy'))
        request.assert_called_with('GET', '/api/fleet/routing/policy')
