import argparse
import json
import unittest
from unittest import mock

from agent_hub_host import agents_cli
from agent_hub_host import cli


def parse(argv):
    parser = argparse.ArgumentParser()
    agents_cli.add_commands(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(argv)


AGENTS_LISTING = {'items': [
    {'id': 'agent:orin', 'display_name': 'Orin', 'forward_session': 'old-session', 'enabled': True, 'machine': 'DESKTOP-FP4OP26'},
    {'id': 'agent:tofu', 'display_name': 'Tofu', 'forward_session': None, 'enabled': True, 'machine': None},
]}


class NamedAgentsCliTest(unittest.TestCase):
    def test_root_agents_command_uses_the_operator_cli_dispatch(self):
        output = []
        with mock.patch.object(cli.sys, 'argv', ['hub', 'agents', 'show', 'orin']), \
                mock.patch.object(agents_cli, 'execute', return_value={'agent': {'id': 'agent:orin'}}) as execute, \
                mock.patch('builtins.print', side_effect=lambda value, *a, **k: output.append(value)):
            cli.main()
        args = execute.call_args.args[0]
        self.assertEqual(args.agents_cmd, 'show')
        self.assertEqual(args.cmd, 'msg')
        self.assertEqual(args.msg_cmd, 'agents')
        self.assertEqual(json.loads(output[-1]), {'agent': {'id': 'agent:orin'}})

    def test_create_registers_a_named_agent_with_optional_machine_pin(self):
        with mock.patch.object(agents_cli.bus, '_do_request', return_value={'agent': {'id': 'agent:orin'}}) as request:
            agents_cli.execute(parse(['agents', 'create', 'orin', '--display', 'Orin', '--machine', 'DESKTOP-FP4OP26']))
            request.assert_called_once_with('POST', '/api/named-agents',
                                            {'name': 'orin', 'display_name': 'Orin', 'machine': 'DESKTOP-FP4OP26'})
        with self.assertRaises(SystemExit):
            # `--display` is required at the parser, so a bare name never posts.
            parse(['agents', 'create', 'orin'])

    def test_show_filters_the_directory_listing(self):
        with mock.patch.object(agents_cli.bus, '_do_request', return_value=AGENTS_LISTING) as request:
            result = agents_cli.execute(parse(['agents', 'show', 'orin']))
            request.assert_called_once_with('GET', '/api/named-agents?limit=50')
            self.assertEqual(result, {'agent': AGENTS_LISTING['items'][0]})
        with mock.patch.object(agents_cli.bus, '_do_request', return_value=AGENTS_LISTING), self.assertRaises(ValueError):
            agents_cli.execute(parse(['agents', 'show', 'missing']))

    def test_forward_upserts_through_the_hub2_named_agents_surface(self):
        with mock.patch.object(agents_cli.bus, '_do_request', return_value=AGENTS_LISTING) as request:
            agents_cli.execute(parse(['agents', 'forward', 'orin', '--to', 'session:11111111-1111-4111-8111-111111111111']))
            request.assert_called_with('POST', '/api/named-agents',
                                       {'name': 'orin', 'display_name': 'Orin', 'enabled': True,
                                        'forward_session': '11111111-1111-4111-8111-111111111111'})
            agents_cli.execute(parse(['agents', 'forward', 'orin', '--clear']))
            request.assert_called_with('POST', '/api/named-agents',
                                       {'name': 'orin', 'display_name': 'Orin', 'enabled': True, 'forward_session': None})
        for bad in ('not-a-session', 'session:'):
            with mock.patch.object(agents_cli.bus, '_do_request', return_value=AGENTS_LISTING), self.assertRaises(ValueError):
                agents_cli.execute(parse(['agents', 'forward', 'orin', '--to', bad]))

    def test_show_follows_pagination_cursors(self):
        pages = [
            {'items': [{'id': 'agent:%02d' % i, 'display_name': str(i)} for i in range(50)], 'next': 'agent:49'},
            {'items': [{'id': 'agent:orin', 'display_name': 'Orin'}], 'next': None},
        ]
        with mock.patch.object(agents_cli.bus, '_do_request', side_effect=pages) as request:
            result = agents_cli.execute(parse(['agents', 'show', 'orin']))
            self.assertEqual(result['agent']['id'], 'agent:orin')
            self.assertEqual(request.call_count, 2)
            self.assertIn('after=agent%3A49', request.call_args.args[1])

    def test_sticky_sets_pins_and_forward_grants(self):
        with mock.patch.object(agents_cli.bus, '_do_request', return_value={'agent': {}}) as request:
            agents_cli.execute(parse(['agents', 'sticky', 'orin', '--machine', 'DESKTOP-FP4OP26']))
            request.assert_called_with('POST', '/api/named-agents/orin/sticky', {'machine': 'DESKTOP-FP4OP26'})
            agents_cli.execute(parse(['agents', 'sticky', 'orin', '--clear-machine']))
            request.assert_called_with('POST', '/api/named-agents/orin/sticky', {'machine': None})
            agents_cli.execute(parse(['agents', 'sticky', 'orin', '--family', 'worker:s:1']))
            request.assert_called_with('POST', '/api/named-agents/orin/sticky', {'family_id': 'worker:s:1'})
            agents_cli.execute(parse(['agents', 'sticky', 'orin', '--family', 'worker:s:1', '--revoke']))
            request.assert_called_with('POST', '/api/named-agents/orin/sticky',
                                       {'family_id': 'worker:s:1', 'revoke': True})
        with self.assertRaises(ValueError):
            agents_cli.execute(parse(['agents', 'sticky', 'orin']))
        with self.assertRaises(ValueError):
            agents_cli.execute(parse(['agents', 'sticky', 'orin', '--machine', 'm', '--clear-machine']))
        with self.assertRaises(ValueError):
            agents_cli.execute(parse(['agents', 'sticky', 'orin', '--revoke']))

    def test_claim_posts_with_the_callers_own_credential(self):
        with mock.patch.object(agents_cli.bus, '_do_request', return_value={'agent': {'id': 'agent:orin'}}) as request:
            agents_cli.execute(parse(['agents', 'claim', 'orin']))
            request.assert_called_once_with('POST', '/api/named-agents/orin/claim', {})


if __name__ == '__main__':
    unittest.main()
