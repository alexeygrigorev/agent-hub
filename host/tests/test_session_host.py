import importlib.util
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from agent_hub_host import session_host

class SessionHostTests(unittest.TestCase):
    def test_fleet_failure_does_not_block_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(session_host.config,'load_config',return_value={'machine_id':'test-machine','access_token':'synthetic'}), patch.object(session_host.fleet,'load_settings',return_value={}), patch.object(session_host,'qualify_sender_binding',return_value=SimpleNamespace(session_id='test-native',workspace=str(pathlib.Path.cwd()))), patch.object(session_host.fleet,'Dispatcher') as dispatcher, patch.object(session_host.session_control,'execute') as execute:
            dispatcher.return_value.once.side_effect=ValueError('unavailable quota')
            session_host.run('synthetic-settings',directory,cycles=1)
            self.assertEqual(execute.call_count,1)
            args=execute.call_args.args[0]
            self.assertEqual(args.machine_id,'test-machine')
            self.assertEqual(args.sessions_cmd,'host-receive')

    def test_unbound_controller_has_no_fleet_or_command_effects(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(session_host.config,'load_config',return_value={'machine_id':'test-machine','access_token':'synthetic'}), patch.object(session_host.fleet,'load_settings',return_value={}), patch.object(session_host,'qualify_sender_binding',side_effect=ValueError('unbound')), patch.object(session_host.fleet,'Dispatcher') as dispatcher, patch.object(session_host.session_control,'execute') as execute:
            with self.assertRaises(ValueError):session_host.run('synthetic-settings',directory,cycles=1)
            dispatcher.assert_not_called();execute.assert_not_called()

    def test_supervisor_identity_rejects_reused_tag_or_engine(self):
        path=pathlib.Path(__file__).resolve().parents[2]/'scripts/session-host-supervisor.py'
        spec=importlib.util.spec_from_file_location('host_supervisor',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        receipt=dict(id='synthetic-id',tag='controller',workspace='/synthetic',engine='shell',created_at_ms=1,command=['python','host'])
        self.assertTrue(module.same_controller(dict(receipt),receipt))
        for key,value in [('id','replacement'),('created_at_ms',2),('engine','opencode'),('command',['another','command'])]:
            changed=dict(receipt);changed[key]=value
            self.assertFalse(module.same_controller(changed,receipt))
