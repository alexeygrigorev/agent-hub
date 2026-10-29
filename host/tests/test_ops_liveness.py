"""`hub ops` payloads for Hub-side orchestrator liveness (intake 500fd60b)."""
import argparse
import unittest
from unittest import mock

from agent_hub_host import ops_liveness


def parse(*argv):
    parser = argparse.ArgumentParser()
    ops_liveness.add_parser(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(['ops', *argv])


class OpsLivenessCliTest(unittest.TestCase):
    def test_watchdog_payload_relays_tick_time_and_failures(self):
        args = parse('heartbeat', '--kind', 'watchdog', '--state', 'proof_failed', '--recovery-failures', '2', '--tick-at', '2026-10-29T02:16:00Z')
        self.assertEqual(ops_liveness.payload(args), {'role': 'orchestrator', 'kind': 'watchdog', 'state': 'proof_failed', 'recovery_failures': 2, 'tick_at': '2026-10-29T02:16:00Z'})

    def test_tick_payload_rejects_watchdog_fields_and_flattens_summary(self):
        self.assertEqual(ops_liveness.payload(parse('heartbeat', '--kind', 'tick', '--summary', 'open 652\nstarted 3')),
                         {'role': 'orchestrator', 'kind': 'tick', 'summary': 'open 652 started 3'})
        with self.assertRaises(ValueError):
            ops_liveness.payload(parse('heartbeat', '--kind', 'tick', '--state', 'ok'))

    def test_routes(self):
        with mock.patch('agent_hub_host.bus._do_request', return_value={'status': 'recorded'}) as req:
            ops_liveness.execute(parse('heartbeat', '--kind', 'tick'))
            req.assert_called_with('POST', '/api/ops/heartbeat', {'role': 'orchestrator', 'kind': 'tick'})
            ops_liveness.execute(parse('status'))
            req.assert_called_with('GET', '/api/ops/liveness')


if __name__ == '__main__':
    unittest.main()
