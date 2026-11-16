"""Offline operator quota contract and actual lazy import-boundary regressions."""
import datetime
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
OBSERVED = '2030-01-01T00:00:00Z'


class FixedDatetime:
    @staticmethod
    def now(tz):
        return NOW


def helper():
    return importlib.import_module('agent_hub_host.quota_metadata')


def project(raw, service='codex', **hints):
    module = helper()
    with patch.object(module, 'dt', types.SimpleNamespace(
            datetime=FixedDatetime, timezone=datetime.timezone)):
        return module.quota_metadata(raw, service, **hints)


def route(**changes):
    value = {'status': 'ok', 'observed_at': OBSERVED,
             'details': {'limit_reached': False},
             'windows': {'7d': {'percent_remaining': 80.0,
                                 'reset_at': '2030-01-08T00:00:00Z',
                                 'present': True, 'rolling': False}}}
    value.update(changes)
    return value


class OperatorQuotaMetadataBoundaryTests(unittest.TestCase):
    def test_provisioning_collection_does_not_import_fleet(self):
        # Fresh interpreter: an earlier fleet import cannot hide the old lazy edge.
        code = textwrap.dedent(r'''
            import contextlib
            import importlib.abc
            from pathlib import Path
            import sys
            from unittest.mock import patch
            sys.path.insert(0, sys.argv[1])
            class NoFleet(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'agent_hub_host.fleet':
                        raise AssertionError('operator quota unexpectedly imports fleet')
            sys.meta_path.insert(0, NoFleet())
            def refused(*args, **kwargs):
                raise AssertionError('no network/provider/subprocess in empty quota fixture')
            with patch('urllib.request.urlopen', side_effect=refused), \
                 patch('urllib.request.OpenerDirector.open', side_effect=refused), \
                 patch('subprocess.Popen', side_effect=refused):
                from agent_hub_host import provisioning as p
                from agent_hub_host import credentials
                assert 'agent_hub_host.fleet' not in sys.modules
                def read_state(path, fallback):
                    assert path == Path('synthetic-empty-root') / 'state.json'
                    assert fallback == []
                    return []
                with patch.object(p, 'lock', return_value=contextlib.nullcontext()) as locked, \
                     patch.object(p, 'root', return_value=Path('synthetic-empty-root')), \
                     patch.object(p, 'read_json', side_effect=read_state) as read, \
                     patch.object(p, 'load_bundle', side_effect=refused), \
                     patch.object(p, 'validate', side_effect=refused), \
                     patch.object(credentials, 'probe_quota', side_effect=refused):
                    assert p.quotas() == {'accounts': []}
                    locked.assert_called_once_with()
                    read.assert_called_once()
                assert 'agent_hub_host.fleet' not in sys.modules
            print('boundary-ok')
        ''')
        completed = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', code,
                                    str(ROOT / 'host')], capture_output=True,
                                   text=True, timeout=20, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), 'boundary-ok')

    def test_fleet_export_is_same_function_with_same_signature(self):
        from agent_hub_host import fleet
        self.assertIs(fleet.quota_metadata, helper().quota_metadata)
        self.assertEqual(str(inspect.signature(fleet.quota_metadata)),
                         '(raw, service, profile=None, account=None)')
        self.assertEqual(fleet.quota_metadata.__defaults__, (None, None))

    def test_provider_selection_and_timestamp_contract(self):
        raw = {'codex': route(windows={'7d': {'percent_remaining': 10}}),
               'codex-orin': route(), 'collector_version': 'quota_snapshot/1.1'}
        for hints in ({'profile': 'hub-codex-orin', 'account': 'codex-orin'},
                      {'profile': 'hub-codex-orin'}):
            result = project(raw, **hints)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['observed_at'], OBSERVED)
            self.assertEqual(result['windows']['7d']['percent_remaining'], 80.0)
            self.assertTrue(result['windows']['7d']['present'])
        self.assertEqual(project(raw)['windows']['7d']['percent_remaining'], 10)
        raw['custom-profile'] = route(windows={'7d': {'percent_remaining': 55}})
        self.assertEqual(project(raw, profile='custom-profile', account='codex-orin')
                         ['windows']['7d']['percent_remaining'], 55)

    def test_unknown_error_stale_and_freshness_never_become_healthy(self):
        for raw in (None, [], {'codex': []}):
            with self.subTest(raw=raw):
                self.assertEqual(project(raw)['status'], 'unknown')
        for changed in ({'stale': True}, {'stale': 'true'}, {'stale': None},
                        {'observed_at': None}, {'observed_at': '2099-01-01T00:00:00Z'},
                        {'error': ''}, {'error': 'SYNTHETIC_PRIVATE'},
                        {'details': {'limit_reached': False, 'stale': True}},
                        {'windows': {'7d': {'percent_remaining': 80, 'stale': True}}}):
            with self.subTest(changed=changed):
                result = project({'codex': route(**changed)})
                self.assertEqual(result['status'], 'unknown')
                self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))

    def test_native_errors_collector_and_field_allowlist_are_preserved(self):
        raw = {'codex': route(status='error', error='unavailable',
                             windows={'5h': {'present': False, 'percent_remaining': None,
                                              'reset_at': None, 'rolling': False,
                                              'error': 'unavailable'}}),
               'collector_version': 'quota_snapshot/1.1'}
        result = project(raw)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['windows']['5h']['error'], 'unavailable')
        self.assertEqual(result['collector_version'], 'quota_snapshot/1.1')
        raw = {'codex': route(details={'limit_reached': False, 'cookie': 'SYNTHETIC_PRIVATE'},
                             windows={'7d': {'percent_remaining': 80,
                                              'raw_stdout': 'SYNTHETIC_PRIVATE'}}),
               'collector_version': 'invalid;version'}
        result = project(raw)
        self.assertNotIn('cookie', result['details'])
        self.assertNotIn('raw_stdout', result['windows']['7d'])
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
        self.assertIsNone(result['collector_version'])
        self.assertEqual(set(result), {'status', 'observed_at', 'details', 'windows',
                                       'collector_version'})


if __name__ == '__main__':
    unittest.main()
