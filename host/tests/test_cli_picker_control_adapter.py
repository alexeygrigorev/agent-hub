import ast
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
MODULE = HERE.parents[1] / 'scripts/hub_alert_evidence.py'
spec = importlib.util.spec_from_file_location('candidate', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = 1800000000000


def entry():
    return {'mode': 'external', 'source_id': 'a' * 64,
            'remote': {'kind': 'picker_control_watermark_v1', 'base_url': 'https://hub.example',
                       'credential_path': '/synthetic/operator.json', 'source_id': 'b' * 64,
                       'release_sha': 'c' * 40}}


def wire():
    return {'recipient': m.RECIPIENT, 'total_picks': 100, 'degraded_picks': 20,
            'consecutive_degraded': 2, 'occurrence_watermark': 5, 'receipt_watermark': 100,
            'coverage': {'available': True, 'epoch': 'synthetic-epoch', 'since': NOW - 10000}}


class AdapterTests(unittest.TestCase):
    def test_exact_existing_shape_produces_real_count_not_generic_controller(self):
        result = m.picker_control_observation(wire(), entry(), observed_at_ms=NOW)
        self.assertEqual(result['occurrence_count'], 5)
        self.assertEqual(result['observed_at_ms'], NOW)
        self.assertEqual(result['kind'], 'picker_control_watermark_observation_v1')
        for invented in ['complete', 'nonce', 'started_total', 'finished_total', 'controller_id', 'artifact_proof']:
            self.assertNotIn(invented, result)

    def test_unknown_unavailable_malformed_counter_fields_refuse(self):
        cases = []
        for key in ['total_picks', 'degraded_picks', 'consecutive_degraded', 'occurrence_watermark', 'receipt_watermark']:
            for bad in [True, 1.0, -1, 2**53, '1', None]:
                value = wire(); value[key] = bad; cases.append(value)
        for bad in [None, {}, {'available': False, 'reason': 'observer_unavailable'},
                    {'available': True, 'epoch': 'ok', 'since': NOW, 'complete': True},
                    {'available': 1, 'epoch': 'ok', 'since': NOW},
                    {'available': True, 'epoch': 'bad\nepoch', 'since': NOW},
                    {'available': True, 'epoch': 'ok', 'since': NOW + 5001}]:
            value = wire(); value['coverage'] = bad; cases.append(value)
        value = wire(); value['recipient'] = 'foreign@example'; cases.append(value)
        value = wire(); value['extra'] = 'opaque'; cases.append(value)
        for value in cases:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(m.EvidenceUnavailable):
                m.picker_control_observation(value, entry(), observed_at_ms=NOW)

    def test_inconsistent_counter_relationships_refuse(self):
        for changes in [dict(degraded_picks=101), dict(consecutive_degraded=21),
                        dict(occurrence_watermark=7), dict(receipt_watermark=99)]:
            with self.subTest(changes=changes), self.assertRaises(m.EvidenceUnavailable):
                m.picker_control_observation({**wire(), **changes}, entry(), observed_at_ms=NOW)

    def test_generation_changes_for_authority_or_epoch_reset_not_normal_growth(self):
        first = m.picker_control_observation(wire(), entry(), observed_at_ms=NOW)
        advanced = wire(); advanced.update(total_picks=101, receipt_watermark=101)
        self.assertEqual(first['generation_component'], m.picker_control_observation(advanced, entry(), observed_at_ms=NOW)['generation_component'])
        for key, changed in [('epoch', 'new-epoch'), ('since', NOW - 1)]:
            value = wire(); value['coverage'][key] = changed
            self.assertNotEqual(first['generation_component'], m.picker_control_observation(value, entry(), observed_at_ms=NOW)['generation_component'])
        changed = entry(); changed['remote']['release_sha'] = 'd' * 40
        self.assertNotEqual(first['generation_component'], m.picker_control_observation(wire(), changed, observed_at_ms=NOW)['generation_component'])

    def test_only_picker_control_can_select_new_contract(self):
        for name in m.REQUIRED_PRODUCERS - {'picker_control'}:
            with self.subTest(name=name), self.assertRaises(m.EvidenceUnavailable):
                m.remote_spec(name, entry())
        self.assertEqual(m.remote_spec('picker_control', entry())['kind'], 'picker_control_watermark_v1')

    def test_bounded_fixed_read_transport_and_secret_not_returned(self):
        token = 'SYNTHETIC_NOT_A_REAL_TOKEN_' * 2
        result = subprocess.CompletedProcess([], 0, json.dumps(wire()).encode())
        with patch.object(m, 'read_private', return_value={'base_url': 'https://hub.example', 'access_token': token}), \
                patch.object(m.subprocess, 'run', return_value=result) as run, \
                patch.object(m.time, 'time', return_value=NOW / 1000):
            observed = m.picker_control_remote(entry())
        args, kw = run.call_args
        self.assertEqual(kw['timeout'], 3)
        self.assertNotIn(token, repr(args))
        self.assertNotIn(token, json.dumps(observed))
        self.assertEqual(json.loads(kw['input'])['url'], 'https://hub.example/api/picker-alerts/counters')
        self.assertNotIn('nonce', json.loads(kw['input']))

    def test_origin_mismatch_timeout_bad_json_oversize_refuse(self):
        with patch.object(m, 'read_private', return_value={'base_url': 'https://foreign.example', 'access_token': 'x' * 40}), \
                patch.object(m.subprocess, 'run') as run:
            with self.assertRaises(m.EvidenceUnavailable): m.picker_control_remote(entry())
            run.assert_not_called()
        credential = {'base_url': 'https://hub.example', 'access_token': 'x' * 40}
        for output in [b'{"recipient":0,"recipient":1}', b'{}', b'x' * (m.LIMIT + 1)]:
            with patch.object(m, 'read_private', return_value=credential), \
                    patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)):
                with self.assertRaises((m.EvidenceUnavailable, ValueError)): m.picker_control_remote(entry())
        with patch.object(m, 'read_private', return_value=credential), \
                patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('synthetic', 3)):
            with self.assertRaises(m.EvidenceUnavailable): m.picker_control_remote(entry())

    def test_fetch_child_actual_code_no_store_and_no_proxy_no_real_network(self):
        ast.parse(m.PICKER_FETCH)
        class Input:
            buffer = io.BytesIO(json.dumps({'url': 'https://hub.example/api/picker-alerts/counters', 'token': 'synthetic'}).encode())
        class Output:
            buffer = io.BytesIO()
        class Response:
            status = 200
            headers = {'Cache-Control': 'private, no-store'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): self.limit = limit; return json.dumps(wire()).encode()
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return response
        response, opener = Response(), Opener()
        import urllib.request
        with patch.object(sys, 'stdin', Input()), patch.object(sys, 'stdout', Output()), \
                patch.object(urllib.request, 'build_opener', return_value=opener) as build:
            exec(compile(m.PICKER_FETCH, '<bounded fetch test>', 'exec'), {})
        self.assertEqual(response.limit, 131073)
        self.assertEqual(opener.request.method, 'GET')
        self.assertEqual(opener.timeout, 1.5)
        self.assertEqual(build.call_args.args[0].proxies, {})
        for bad in ['', 'public, max-age=60', 'x-no-store']:
            response.headers = {'Cache-Control': bad}
            Input.buffer = io.BytesIO(json.dumps({'url': 'https://hub.example/api/picker-alerts/counters', 'token': 'synthetic'}).encode())
            with patch.object(sys, 'stdin', Input()), patch.object(sys, 'stdout', Output()), \
                    patch.object(urllib.request, 'build_opener', return_value=opener):
                with self.assertRaises(SystemExit) as error:
                    exec(compile(m.PICKER_FETCH, '<bounded fetch test>', 'exec'), {})
            self.assertEqual(error.exception.code, 2)

    def test_generic_witness_cannot_accept_picker_observation(self):
        observed = m.picker_control_observation(wire(), entry(), observed_at_ms=NOW)
        with self.assertRaises(m.EvidenceUnavailable):
            m.witness('picker_control', entry(), now_ms=NOW, remote_value=observed)

    def test_actual_aggregate_branch_preserves_other_six_and_generation(self):
        entries = {}
        for name in m.REQUIRED_PRODUCERS:
            entries[name] = {'mode': 'external', 'source_id': 'f' * 64,
                             'witness_path': '/synthetic/' + name}
        entries['picker_control'] = entry()
        registry = {'kind': 'hub_alert_enrollment_v1', 'recipient': m.RECIPIENT, 'producers': entries}
        class MemoryStore:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def lock(self): return contextlib.nullcontext()
            def read(self, name):
                if name == 'enrollment.json': return copy.deepcopy(registry)
                raise AssertionError('unexpected read')
        def witness(path):
            name = path.rsplit('/', 1)[1]
            return {'kind': 'hub_controller_coverage_v1', 'producer': name,
                    'source_id': 'f' * 64, 'epoch': 'synthetic-epoch', 'controller_id': 'synthetic-controller',
                    'limitations': [], 'complete': True, 'started_total': 1, 'finished_total': 1,
                    'baseline_count': 0, 'baseline_invocations': 0, 'occurrence_count': 2, 'observed_at_ms': NOW}
        raw = wire()
        def observed(_entry):
            return m.picker_control_observation(raw, _entry, observed_at_ms=NOW)
        with patch.object(m, 'PrivateDirectory', MemoryStore), patch.object(m, 'verify_source'), \
                patch.object(m, 'read_private', side_effect=witness), patch.object(m, 'picker_control_remote', side_effect=observed):
            baseline = m.aggregate('/synthetic', now_ms=NOW)
            self.assertEqual(baseline['kind'], 'dev_alert_occurrences_v1')
            self.assertEqual(baseline['count'], 6 * 2 + 5)
            raw['occurrence_watermark'] = 6
            final = m.aggregate('/synthetic', now_ms=NOW)
            self.assertEqual(final['count'], baseline['count'] + 1)
            self.assertEqual(final['generation'], baseline['generation'])
            raw['coverage']['epoch'] = 'new-epoch'
            self.assertNotEqual(m.aggregate('/synthetic', now_ms=NOW)['generation'], baseline['generation'])
            del entries['backup']
            self.assertEqual(m.aggregate('/synthetic', now_ms=NOW)['kind'], 'hub_alert_evidence_unavailable_v1')

    def test_aggregate_does_not_heal_unavailable_other_producer(self):
        # Existing generic witness schema remains strict, independently of the
        # new committed-draw authority; unavailable is not assigned zero.
        value = {'kind': 'hub_controller_coverage_v1', 'producer': 'backup',
                 'source_id': 'f' * 64, 'epoch': 'epoch', 'controller_id': 'controller',
                 'limitations': ['missing-invocation'], 'complete': False}
        with patch.object(m, 'read_private', return_value=value), self.assertRaises(m.EvidenceUnavailable):
            m.witness('backup', {'source_id': 'f' * 64, 'witness_path': '/synthetic/backup'}, now_ms=NOW)


if __name__ == '__main__':
    unittest.main(verbosity=2)
