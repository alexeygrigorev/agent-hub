"""Native all-registry census validation with captured API shapes; no launches."""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host.native_inventory import collect_native_inventory
from agent_hub_host.native_launch import NativeLauncher


class NativeInventoryTests(unittest.TestCase):
    def native_row(self):
        fixture = Path(__file__).resolve().parents[2] / 'tests/e2e/fixtures/aplexer.json'
        row = json.loads(fixture.read_text())['status_running']
        return {**row, 'id': '11111111-1111-4111-8111-111111111111',
                'workspace': str(Path.cwd()), 'tag': 'task-hub2-known', 'schema_version': 1,
                'env': {'SECRET': 'must-not-survive'}, 'command': ['private-prompt']}

    def query(self, rows, returncode=0):
        return lambda: subprocess.CompletedProcess(['a'], returncode, json.dumps(rows), '')

    def test_actual_native_entry_requests_full_array_and_excludes_private_data(self):
        row = self.native_row()
        calls = []
        def runner(args, **options):
            calls.append((args, options))
            return subprocess.CompletedProcess(args, 0, json.dumps([row]), '')
        launcher = NativeLauncher({'private_provider_homes': True}, runner, aplexer=sys.executable)
        inventory = launcher.inventory()
        self.assertEqual(calls[0][0], [sys.executable, 'list', '--all', '--json'])
        self.assertEqual(calls[0][1]['timeout'], 5)
        self.assertIsNone(calls[0][1]['cwd'])
        self.assertEqual(len(inventory.identities), 1)
        self.assertNotIn('SECRET', str(inventory))
        self.assertNotIn('private-prompt', str(inventory))
        self.assertIsNone(inventory.absence_evidence(row['workspace'], row['tag']))
        evidence = inventory.absence_evidence(row['workspace'], 'task-hub2-missing')
        self.assertTrue(evidence['complete'])
        self.assertEqual(evidence['row_count'], 1)
        self.assertEqual(evidence['dispatch_provenance'], 'legacy_unknown')
        self.assertNotIn('native_exit_confirmed', evidence)

    def test_bad_envelopes_rows_and_safety_bounds_never_produce_absence(self):
        row = self.native_row()
        cases = [None, {'sessions': []}, [row, row], [dict(row, id='prefix')],
                 [dict(row, workspace='relative')], [dict(row, workspace='/bad\0path')],
                 [dict(row, schema_version=True)]]
        for value in cases:
            with self.subTest(value=str(value)[:80]), self.assertRaisesRegex(RuntimeError, 'native_census_unavailable'):
                collect_native_inventory(self.query(value))
        with self.assertRaisesRegex(RuntimeError, 'native_census_unavailable'):
            collect_native_inventory(self.query([], 1))
        with patch('agent_hub_host.native_inventory.MAX_CENSUS_ROWS', 0), self.assertRaises(RuntimeError):
            collect_native_inventory(self.query([row]))
        with patch('agent_hub_host.native_inventory.MAX_CENSUS_BYTES', 1), self.assertRaises(RuntimeError):
            collect_native_inventory(self.query([]))

    def test_deleted_workspace_matches_and_a_stale_snapshot_cannot_close(self):
        workspace = str(Path.cwd() / 'already-deleted-workspace')
        row = {**self.native_row(), 'workspace': workspace + ' (deleted)'}
        inventory = collect_native_inventory(self.query([row]))
        self.assertIsNone(inventory.absence_evidence(workspace, row['tag']))
        with patch('agent_hub_host.native_inventory.time.monotonic', return_value=inventory.observed_monotonic+6):
            with self.assertRaisesRegex(RuntimeError, 'native_census_stale'):
                inventory.absence_evidence(workspace, 'task-hub2-missing')


if __name__ == '__main__':
    unittest.main()
