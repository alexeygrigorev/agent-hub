from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub_host import repos


class RepoHostPolicyCliTests(unittest.TestCase):
    def test_operator_edit_sends_restriction_reset_or_empty_set(self):
        args = SimpleNamespace(repos_cmd='edit',name='demo',file='operator.json')
        for value in ('["tabo"]','null','[]'):
            with self.subTest(value=value), patch.object(repos.intake,'read_file',return_value='{"allowed_machines":'+value+'}'), \
                    patch.object(repos.intake,'request',return_value={'ok':True}) as send:
                self.assertEqual(repos.execute(args),{'ok':True})
                self.assertEqual(send.call_args.args[:2],('PATCH','/api/repos/demo'))

    def test_bad_host_policy_is_refused_before_network(self):
        args = SimpleNamespace(repos_cmd='edit',name='demo',file='operator.json')
        for value in ('"tabo"','["tabo","tabo"]','["../bad"]','[3]','{}'):
            with self.subTest(value=value), patch.object(repos.intake,'read_file',return_value='{"allowed_machines":'+value+'}'), \
                    patch.object(repos.intake,'request') as send:
                with self.assertRaisesRegex(ValueError,'invalid_repo_fields'):
                    repos.execute(args)
                send.assert_not_called()
