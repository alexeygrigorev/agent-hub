import argparse
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub_host import agents_cli
from agent_hub_host import cli


def parse(argv):
    parser = argparse.ArgumentParser()
    agents_cli.add_commands(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(argv)


class NamedAgentsCliTest(unittest.TestCase):
    def test_root_agents_command_uses_the_operator_cli_dispatch(self):
        output = []
        with mock.patch.object(cli.sys, 'argv', ['hub', 'agents', 'show', 'developer']), \
                mock.patch.object(agents_cli, 'execute', return_value={'actor': 'agent:developer'}) as execute, \
                mock.patch('builtins.print', side_effect=lambda value, *args, **kwargs: output.append(value)):
            cli.main()
        args = execute.call_args.args[0]
        self.assertEqual(args.agents_cmd, 'show')
        self.assertEqual(args.cmd, 'msg')
        self.assertEqual(args.msg_cmd, 'agents')
        self.assertEqual(json.loads(output[-1]), {'actor': 'agent:developer'})

    def test_create_uses_separate_private_profile_and_never_prints_tokens_or_overwrites_human(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp}), \
                mock.patch.object(agents_cli, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                mock.patch.object(agents_cli.bus, '_do_request', return_value={'actor': 'agent:developer', 'access_token': 'private-token', 'refresh_token': 'private-refresh'}) as request:
            os.environ.pop('HUB_CONFIG_FILE', None)
            result = agents_cli.execute(parse(['agents', 'create', 'developer']))
            path = Path(result['written'])
            self.assertEqual(path.name, 'developer.json')
            self.assertEqual(json.loads(path.read_text())['actor'], 'agent:developer')
            self.assertNotIn('private-token', json.dumps(result))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(request.call_args.args[:2], ('POST', '/api/agents/named/developer/create'))
            with self.assertRaises(ValueError):
                agents_cli.execute(parse(['agents', 'create', 'developer']))
            with self.assertRaises(ValueError):
                agents_cli.execute(parse(['agents', 'create', 'cli']))
            self.assertEqual(request.call_count, 1)

    def test_delegate_and_forward_use_operator_endpoint_and_exact_target(self):
        with mock.patch.object(agents_cli.bus, '_do_request', return_value={}) as request:
            agents_cli.execute(parse(['agents', 'delegate', 'developer', '--mcp-client', 'client-a']))
            request.assert_called_with('POST', '/api/agents/named/developer/delegate', {'mcp_client': 'client-a'})
            agents_cli.execute(parse(['agents', 'forward', 'oncall', '--to', 'session:abc']))
            request.assert_called_with('POST', '/api/agents/named/oncall/forward', {'to': 'session:abc'})
            agents_cli.execute(parse(['agents', 'forward', 'oncall', '--clear']))
            request.assert_called_with('POST', '/api/agents/named/oncall/forward', {'to': None})


if __name__ == '__main__':
    unittest.main()
