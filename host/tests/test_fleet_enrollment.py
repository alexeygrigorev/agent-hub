import argparse
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import auth, cli, config


class EnrollmentTests(unittest.TestCase):
    def test_host_login_requests_current_server_scopes(self):
        with patch.object(auth, 'device_login') as login, patch('agent_hub_host.system.get_hostname', return_value='test-host'):
            cli.cmd_login(argparse.Namespace(base_url='https://hub.example', kind='host', name=None, scope=None))
        self.assertEqual(login.call_args.args[1], 'host')
        self.assertEqual(set(login.call_args.args[-1]), {'host', 'bus:send', 'bus:read', 'health:report'})
        self.assertNotIn('host_agent', login.call_args.args[-1])

    def test_agent_login_does_not_inherit_host_or_spawn_scope(self):
        with patch.object(auth, 'device_login') as login:
            cli.cmd_login(argparse.Namespace(base_url='https://hub.example', kind='agent', name='grok-bot', scope=None))
        self.assertEqual(login.call_args.args[1:3], ('agent', 'grok-bot'))
        self.assertEqual(set(login.call_args.args[-1]), {'bus:send', 'bus:read'})
        with patch.object(auth, 'device_login') as login:
            cli.cmd_login(argparse.Namespace(base_url='https://hub.example', kind='agent', name='coordinator', scope=['bus:read', 'fleet:launch']))
        self.assertEqual(login.call_args.args[-1], ['bus:read', 'fleet:launch'])

    def test_distinct_agent_profile_does_not_change_default_host_path(self):
        override = str(Path.cwd() / 'private-agent.json')
        with patch.dict(os.environ, {'HUB_CONFIG_FILE': override}):
            self.assertEqual(config.get_config_path(), Path(override))
        with patch.dict(os.environ, {'HUB_CONFIG_FILE': 'relative.json'}):
            with self.assertRaises(ValueError): config.get_config_path()


if __name__ == '__main__': unittest.main()
