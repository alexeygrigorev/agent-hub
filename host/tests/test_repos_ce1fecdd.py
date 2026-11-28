from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub_host import repos


class RepoHostPolicyCliTests(unittest.TestCase):
    """The Hub1 allowed_machines host policy has no Hub2 route. The Hub2 CLI
    must refuse the legacy field loudly before any network so the operator is
    never led to believe a restriction was stored (intake ce1fecdd contract,
    superseded on Hub2)."""

    def test_legacy_host_policy_field_is_refused_before_network(self):
        args = SimpleNamespace(repos_cmd='edit', name='Example/demo', file='operator.json')
        for value in ('["tabo"]', 'null', '[]'):
            with self.subTest(value=value), patch.object(repos.intake, 'read_file', return_value='{"allowed_machines":' + value + '}'), \
                    patch.object(repos.intake, 'request') as send:
                with self.assertRaisesRegex(ValueError, 'Unknown repository fields: allowed_machines'):
                    repos.execute(args)
                send.assert_not_called()

    def test_other_legacy_human_fields_are_refused_before_network(self):
        args = SimpleNamespace(repos_cmd='edit', name='Example/demo', file='operator.json')
        for field in ('label', 'stack', 'key_folders', 'urls', 'canonical'):
            with self.subTest(field=field), patch.object(repos.intake, 'read_file', return_value='{"' + field + '":"x"}'), \
                    patch.object(repos.intake, 'request') as send:
                with self.assertRaisesRegex(ValueError, 'Unknown repository fields: ' + field):
                    repos.execute(args)
                send.assert_not_called()
