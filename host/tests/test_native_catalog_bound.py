"""Complete catalogs must survive pretty-print overhead without weakening bounds."""
import json
import subprocess
import unittest
from unittest.mock import patch

from agent_hub_host.native_sessions import run_native, SessionUncertain, MAX_NATIVE_OUTPUT, MAX_NATIVE_CATALOG_OUTPUT


class NativeCatalogBound(unittest.TestCase):
    def call(self, text, args=None, *, stderr='', sdk=False):
        args = args or ['list', '--all', '--json']
        result = subprocess.CompletedProcess(['aplexer', *args], 0, text, stderr)
        with patch('agent_hub_host.aplexer_client.client_call', return_value=result if sdk else None), \
                patch('agent_hub_host.native_sessions.subprocess.run', return_value=result):
            return run_native('/explicit/native/aplexer', args)

    def test_complete_large_pretty_catalog_survives_cli_and_sdk_paths(self):
        rows = [dict(id=str(i), tag='task-' + str(i), workspace='/repo',
                     nested=dict(values=[dict(key='é', metadata='kept')] * 8)) for i in range(1100)]
        pretty = json.dumps(rows, ensure_ascii=False, indent=4)
        self.assertGreater(len(pretty.encode()), MAX_NATIVE_OUTPUT)
        self.assertLessEqual(len(pretty.encode()), 2 * MAX_NATIVE_OUTPUT)
        for sdk in [False, True]:
            with self.subTest(sdk=sdk):
                proc = self.call(pretty, sdk=sdk)
                self.assertLessEqual(len(proc.stdout.encode()), MAX_NATIVE_OUTPUT)
                self.assertEqual(json.loads(proc.stdout), rows)

    def test_large_mutation_and_error_output_keep_existing_bound(self):
        for args in [['kill', 'own-id'], ['status', 'own-id', '--json'], ['start', '--json']]:
            with self.subTest(args=args), self.assertRaises(SessionUncertain):
                self.call(' ' * (MAX_NATIVE_OUTPUT + 1), args)
        with self.assertRaises(SessionUncertain):
            self.call('[]', stderr='x' * (MAX_NATIVE_OUTPUT + 1))

    def test_complete_catalog_above_ordinary_bound_uses_existing_fleet_budget(self):
        # An already compact catalog cannot be repaired by whitespace removal.
        # Keep all containment/identity metadata rather than dropping rows.
        rows = [dict(id='one', tag='task-one', workspace='/repo',
                     containment=dict(metadata='x' * (MAX_NATIVE_OUTPUT + 1)))]
        text = json.dumps(rows, separators=(',', ':'))
        for sdk in [False, True]:
            with self.subTest(sdk=sdk):
                proc = self.call(text, sdk=sdk)
                self.assertEqual(json.loads(proc.stdout), rows)
                self.assertLessEqual(len(proc.stdout.encode()), MAX_NATIVE_CATALOG_OUTPUT)

    def test_oversized_malformed_or_lossy_catalog_is_never_partial_evidence(self):
        padding = ' ' * MAX_NATIVE_OUTPUT
        for text in [padding + '[{"id":1,"id":2}]', padding + '[NaN]', padding + '{"rows":[]}',
                     padding + '[broken', ' ' * (MAX_NATIVE_CATALOG_OUTPUT + 1),
                     '[{"text":"' + 'x' * MAX_NATIVE_CATALOG_OUTPUT + '"}]']:
            with self.subTest(size=len(text)), self.assertRaises(SessionUncertain):
                self.call(text)


if __name__ == '__main__':
    unittest.main()
