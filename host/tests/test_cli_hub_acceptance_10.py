"""Offline tests for scripts/hub_acceptance_10.py.

Fake `hub`, `a` and alert-sample binaries are put on PATH; their responses model the maintained CLI contract with synthetic fixtures in
host/tests/fixtures/acceptance/. No Hub access, no real session is started or stopped."""
import importlib.util
import io
import json
import re
import hashlib
import os
import runpy
import selectors
import subprocess
import sys
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIX = HERE / 'fixtures' / 'acceptance'
spec = importlib.util.spec_from_file_location('acc10', HERE.parents[1] / 'scripts/hub_acceptance_10.py')
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)

SOL, GLM, MUSE, LUNA = 'claude-sonnet-4-6', 'claude-opus-4-6', 'gemini-4-hydrogen', 'gemini-4-luna'
HAIKU = 'claude-haiku-4-5'
# Offline PASS-path draws here use the qualified plain-pane contract (Claude). The qualified Hydrogen medium
# styled-screen path is exercised in test_cli_hydrogen_acceptance.py; MUSE/LUNA below are OTHER antigravity models
# that must keep failing with UNKNOWN_NATIVE_FRAMING. The runtime picker itself is NOT narrowed.
OK_MODELS = [SOL, GLM, HAIKU, SOL, GLM, HAIKU, SOL, GLM, HAIKU, SOL]
NATIVE_POOL = [
    {'policy_id': 'claude-sonnet', 'engine': 'claude', 'model': SOL, 'effort': 'medium', 'weight': 30, 'share': 0.3},
    {'policy_id': 'claude-opus', 'engine': 'claude', 'model': GLM, 'effort': 'high', 'weight': 20, 'share': 0.2},
    {'policy_id': 'claude-haiku', 'engine': 'claude', 'model': HAIKU, 'effort': 'low', 'weight': 10, 'share': 0.1},
    {'policy_id': 'hydrogen-med', 'engine': 'antigravity', 'model': MUSE, 'effort': 'medium', 'weight': 25, 'share': 0.25},
    {'policy_id': 'hydrogen-luna', 'engine': 'antigravity', 'model': LUNA, 'effort': 'low', 'weight': 15, 'share': 0.15},
]
_CLAUDE = [dict(x) for x in NATIVE_POOL if x['engine'] == 'claude']
CLAUDE_POOL = [dict(x, share=x['weight'] / sum(y['weight'] for y in _CLAUDE)) for x in _CLAUDE]
PARENT_INTAKE = '11111111-1111-4111-8111-111111111111'


def fixture(name):
    return json.loads((FIX / name).read_text())


COMMIT = 'a' * 40
TMP = HERE.parents[1] / '.local/tmp'
TMP.mkdir(parents=True, exist_ok=True)


def transport(engine='codex', commit=COMMIT):
    # Legacy headless stream transport fixture (for legacy ParserTests only).
    return {'engine': engine, 'machine_id': acc.HOST,
            'format': acc.CAPTURE_FORMATS.get(engine, 'plaintext'), 'host_release_sha': commit,
            'provider_version': 'offline-fixture-v1',
            'profile_set_sha256': hashlib.sha256(('nonsecret-offline-argv-' + engine).encode()).hexdigest(),
            'reference': 'synthetic://capture/' + engine,
            'sha256': hashlib.sha256(('offline-capture-receipt-' + engine).encode()).hexdigest()}


def native_transport(engine='claude', commit=COMMIT):
    return {
        'engine': engine,
        'machine_id': acc.HOST,
        'mode': 'interactive',
        'format': acc.NATIVE_CAPTURE_FORMATS[engine],
        # A hypothetical/new Hydrogen class label must never be enough (label is not attestation).
        'proof_class': acc.NATIVE_PROOF_CLASSES.get(engine, 'native_exact_probe_header_plus_artifact_v3'),
        'capture_command': acc.NATIVE_CAPTURE_COMMAND,
        'artifact_schema': acc.WORK_SCHEMA,
        'host_release_sha': commit,
        'provider_version': 'offline-native-fixture-v2',
        'profile_set_sha256': hashlib.sha256(('nonsecret-interactive-argv-' + engine).encode()).hexdigest(),
        'reference': 'synthetic://native-capture/' + engine,
        'sha256': hashlib.sha256(('offline-native-receipt-' + engine).encode()).hexdigest(),
    }


def provider_frames(engine, marker):
    session = '11111111-2222-4333-8444-555555555555'
    if engine == 'claude':
        return [
            {'type': 'assistant', 'session_id': session, 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'toolu_synthetic_print', 'name': 'Bash', 'input': {'command': 'python3 synthetic_print.py'}}]}},
            {'type': 'user', 'session_id': session, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_synthetic_print', 'content': marker + '\n', 'is_error': False}]},
             'tool_use_result': {'stdout': marker + '\n', 'stderr': '', 'interrupted': False}},
        ]
    params = {'CommandLine': 'python3 synthetic_print.py'}
    return [
        {'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 7, 'step_type': 'tool',
         'state': 'ACTIVE', 'tool_name': 'run_command', 'tool_info': {'name': 'run_command', 'parameters': dict(params)}}},
        {'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 7, 'step_type': 'tool',
         'state': 'DONE', 'tool_name': 'run_command', 'tool_info': {'name': 'run_command', 'parameters': dict(params),
         'output': 'Created At: 2026-01-01T00:00:00+00:00\nCompleted At: 2026-01-01T00:00:01+00:00\n'
                   'Command exited with code 0.\n\nCommand output:\n' + marker + '\n'}}},
    ]


def jsonl(events):
    return '\n'.join(json.dumps(event) for event in events)


def evidence(commit=COMMIT, **kw):
    ev = {'kind': 'phase7_operator_validated_evidence_v1',
          'canary': 'success', 'canary_commit': commit, 'ci_e2e': 'success', 'ci_e2e_commit': commit,
          'operator_validation': {'actor': 'offline-fixture', 'target_commit': commit,
              'capture_transports': {engine: transport(engine, commit) for engine in acc.CAPTURE_FORMATS}, 'receipts': {
              name: {'commit': commit, 'reference': 'synthetic://' + name + '/receipt',
                     'sha256': hashlib.sha256(('offline-' + name).encode()).hexdigest()}
              for name in ('deployment', 'canary', 'ci_e2e')}}}
    ev.update(kw)
    f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, dir=TMP)
    json.dump(ev, f)
    f.close()
    unittest.addModuleCleanup(os.unlink, f.name)
    return f.name


def native_evidence(commit=COMMIT, **kw):
    ev = {
        'kind': acc.NATIVE_EVIDENCE_KIND,
        'canary': 'success',
        'canary_commit': commit,
        'ci_e2e': 'success',
        'ci_e2e_commit': commit,
        'operator_validation': {
            'actor': 'offline-fixture',
            'target_commit': commit,
            # Plain-pane (Claude) attestation only; the Hydrogen styled-screen record is a separate shape
            # (host/tests/test_cli_hydrogen_acceptance.py).
            'capture_transports': {'claude': native_transport('claude', commit)},
            'receipts': {
                name: {
                    'commit': commit,
                    'reference': 'synthetic://' + name + '/receipt',
                    'sha256': hashlib.sha256(('offline-' + name).encode()).hexdigest(),
                }
                for name in ('deployment', 'canary', 'ci_e2e')
            },
        },
    }
    ev.update(kw)
    f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, dir=TMP)
    json.dump(ev, f)
    f.close()
    unittest.addModuleCleanup(os.unlink, f.name)
    return f.name


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='acc10-', dir=TMP))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for name in ('hub', 'a', 'git', 'alerts'):
            (self.bin / name).symlink_to(FIX / 'fake_hub.py')
        self.base_ws = self.tmp / 'base-ws'
        self.base_ws.mkdir()
        self.child_ws_root = self.tmp / 'child-ws'
        self.child_ws_root.mkdir()
        self.cfg_path = self.tmp / 'cfg.json'
        env = {'PATH': '%s:%s' % (self.bin, os.environ['PATH']), 'FAKE_CFG': str(self.cfg_path)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, val in (('CAPTURE_BUDGET_S', 2), ('CREATE_BUDGET_S', 10)):
            p = mock.patch.object(acc, name, val)
            p.start()
            self.addCleanup(p.stop)

    def state(self):
        path = Path(str(self.cfg_path) + '.state')
        return json.loads(path.read_text()) if path.exists() else {'sessions': {}, 'intakes': {}, 'log': []}

    def go(self, **cfg):
        bundle = cfg.pop('evidence_bundle', None)
        out_dir = cfg.pop('output_dir', self.tmp / ('out-' + str(time.time_ns())))
        cfg.setdefault('models', OK_MODELS)
        cfg.setdefault('pool', NATIVE_POOL)
        cfg.setdefault('child_workspace_root', str(self.child_ws_root))
        Path(str(self.cfg_path) + '.state').unlink(missing_ok=True)
        self.cfg_path.write_text(json.dumps(cfg))
        out = io.StringIO()
        r = acc.Runner(alerts_cmd=str(self.bin / 'alerts'))
        rc = acc.run(
            r, bundle or native_evidence(), COMMIT,
            workspace=str(self.base_ws), sleep=lambda s: time.sleep(0.01),
            out=out, intake=PARENT_INTAKE, output_dir=str(out_dir),
        )
        return rc, out.getvalue()

    def assertAllStopped(self):
        st = self.state()
        self.assertTrue(st['sessions'])
        for s in st['sessions'].values():
            self.assertTrue(s['stopped'], 'leaked session %s' % s['id'])


class FixtureStateTests(Harness):
    def seed_state(self):
        self.cfg_path.write_text('{}')
        state = {'n': 0, 'sessions': {}, 'keys': {}, 'intakes': {}, 'intake_keys': {}, 'log': []}
        path = Path(str(self.cfg_path) + '.state')
        path.write_text(json.dumps(state))
        return path, path.read_bytes()

    def test_failed_atomic_publication_preserves_previous_state_and_removes_temporary(self):
        path, previous = self.seed_state()
        with mock.patch.object(os, 'replace', side_effect=OSError('synthetic publication failure')):
            with self.assertRaisesRegex(OSError, 'synthetic publication failure'):
                runpy.run_path(str(FIX / 'fake_hub.py'))
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.state()['sessions'], {})
        self.assertEqual(list(path.parent.glob(path.name + '.*')), [])

    def test_killed_publication_preserves_readable_state_for_next_cli_invocation(self):
        path, previous = self.seed_state()
        code = """
import os, runpy, signal, sys

def interrupted_replace(source, destination):
    print('publication-ready', flush=True)
    signal.pause()

os.replace = interrupted_replace
runpy.run_path(sys.argv[1])
"""
        child = subprocess.Popen([sys.executable, '-c', code, str(FIX / 'fake_hub.py')],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), 'fixture did not reach publication barrier')
            self.assertEqual(child.stdout.readline().strip(), 'publication-ready')
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(self.state()['sessions'], {})
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(path.read_bytes(), previous)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        result = subprocess.run([sys.executable, str(FIX / 'fake_hub.py')],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['count'], 40)
        self.assertEqual(len(self.state()['watermarks']), 1)
        self.assertNotEqual(path.read_bytes(), previous)


class AcceptanceTests(Harness):
    def test_pass_prints_shares_stops_all_and_passes_intake(self):
        rc, out = self.go()
        self.assertEqual(rc, 0, out)
        self.assertIn('ACCEPTANCE PASS', out)
        self.assertEqual(out.count('shares(session.picker)='), 10)
        self.assertAllStopped()
        self.assertEqual(len(self.state()['sessions']), 10)
        self.assertEqual(len(self.state()['intakes']), 10)
        self.assertNotIn('pick', self.state()['log'])
        summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
        self.assertEqual(len(summary['starts'][0]['picker']['pool']), len(NATIVE_POOL))
        self.assertEqual(summary['starts'][0]['picker']['policy_version'], 7)
        for rec in summary['starts']:
            self.assertEqual(rec['requested_workspace'], str(self.base_ws))
            self.assertNotEqual(rec['workspace'], str(self.base_ws))
            self.assertTrue(rec['archived'])
            self.assertEqual(rec['cleanup_status'], 'CONFIRMED')
        sequence = [x for x in self.state()['log'] if x == 'sessions:start' or x.startswith('stop:')]
        self.assertEqual(sequence[::2], ['sessions:start'] * 10)

    def test_wrong_show_identity_is_refused_and_known_start_is_stopped(self):
        rc, out = self.go(wrong_show_id=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('show session id mismatch', out)
        self.assertAllStopped()
        self.assertEqual(self.state()['n'], 1)

    def test_malformed_native_uuid_never_proves_presence_or_cleanup_absence(self):
        for value in ('native-placeholder', '1234', 'AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE', [], {'id': 'untrusted'}):
            with self.subTest(value=value):
                rc, out = self.go(native_id=value)
                self.assertEqual(rc, 1, out)
                self.assertIn('no canonical native UUID', out)
                self.assertAllStopped()
                self.assertEqual(self.state()['n'], 1)

    def test_reused_session_and_native_identities_end_the_consecutive_run(self):
        for knob, message in [('reuse_session_id', 'session id reused'), ('reuse_native_id', 'native UUID reused')]:
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn(message, out)
                self.assertAllStopped()
                self.assertEqual(self.state()['n'], 2)

    def test_assistant_or_result_nonce_is_not_completed_tool_output(self):
        for knob in ('capture_assistant_only', 'capture_result_only'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn('did not prove completed work', out)
                self.assertAllStopped()

    def test_foreign_plaintext_json_and_missing_attestation_fail_with_cleanup(self):
        rc, out = self.go(capture_format_override='go')
        self.assertEqual(rc, 1, out)
        self.assertIn('did not prove completed work', out)
        self.assertAllStopped()
        path = Path(native_evidence())
        ev = json.loads(path.read_text())
        ev['operator_validation']['capture_transports'].pop('claude')
        path.write_text(json.dumps(ev))
        rc, out = self.go(evidence_bundle=str(path))
        self.assertEqual(rc, 1, out)
        self.assertIn('verified interactive native-pane capture transport records required', out)
        self.assertEqual(self.state()['sessions'], {})

    def test_operator_bundle_must_not_change_during_acceptance(self):
        original = acc.checked_native_evidence
        calls = []
        def check(path, commit):
            calls.append(path)
            if len(calls) == 2:
                data = json.loads(Path(path).read_text())
                data['operator_validation']['actor'] = 'different-offline-validator'
                Path(path).write_text(json.dumps(data))
            return original(path, commit)
        with mock.patch.object(acc, 'checked_native_evidence', side_effect=check):
            rc, out = self.go()
        self.assertEqual(rc, 1, out)
        self.assertIn('evidence bundle changed during run', out)
        self.assertAllStopped()

    def test_replacement_after_validation_cannot_become_the_baseline_hash(self):
        original = acc.validate_native_evidence
        pins = {}
        def validate_then_replace(data, path, commit):
            problems = original(data, path, commit)
            if not pins:
                pins['validated'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                replacement = json.loads(Path(path).read_text())
                replacement['operator_validation']['actor'] = 'replacement-after-prevalidation'
                Path(path).write_text(json.dumps(replacement))
                pins['replacement'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            return problems
        with mock.patch.object(acc, 'validate_native_evidence', side_effect=validate_then_replace):
            rc, out = self.go()
        self.assertEqual(rc, 1, out)
        self.assertIn('evidence bundle changed during run', out)
        summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
        self.assertEqual(summary['evidence_sha256'], pins['validated'])
        self.assertNotEqual(pins['validated'], pins['replacement'])
        self.assertEqual(self.state()['n'], 10)
        self.assertAllStopped()

    def test_same_attempt_snapshot_must_not_change(self):
        rc, out = self.go(show_pool_changed=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('same attempt picker evidence changed', out)
        self.assertAllStopped()

    def test_actual_current_fallback_pool_and_history(self):
        # Fallback stays inside qualified-framing engines here; a Hydrogen fallback is covered by the B2 tests.
        rc, out = self.go(fallback=True, pool=CLAUDE_POOL)
        self.assertEqual(rc, 0, out)
        self.assertAllStopped()
        rc, out = self.go(fallback=True, cross_attempt_pool=True, pool=CLAUDE_POOL)
        self.assertEqual(rc, 1)
        self.assertIn('current chosen model/policy not in the picker pool', out)
        self.assertAllStopped()

    def test_missing_or_mismatching_actual_evidence_fails_closed(self):
        for knob, message in [('missing_picker', 'picker evidence missing'),
                              ('show_missing_picker', 'picker evidence missing'),
                              ('missing_version', 'policy version missing'),
                              ('picker_model_mismatch', 'model mismatch'),
                              ('initial_offered', 'default waited start did not return running')]:
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn(message, out)
                self.assertAllStopped()
                self.assertNotIn('pick', self.state()['log'])

    def test_refuses_without_ready_env(self):
        os.environ.pop('HUB_ACCEPTANCE_READY', None)
        self.assertEqual(acc.main(['--live']), 2)
        self.assertEqual(acc.main([]), 2)

    def test_acceptance_criteria_source_required(self):
        self.cfg_path.write_text(json.dumps({'models': OK_MODELS}))
        out = io.StringIO()
        rc = acc.run(
            acc.Runner(alerts_cmd=str(self.bin / 'alerts')), native_evidence(), COMMIT,
            workspace=str(self.base_ws), out=out, output_dir=str(self.tmp / 'out'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('valid parent --intake-task-id UUID required', out.getvalue())
        self.assertEqual(self.state()['sessions'], {}, 'must not start anything')

    def test_start_failure_ends_run(self):
        rc, out = self.go(fail_start_at=3)
        self.assertEqual(rc, 1)
        self.assertIn('start failed', out)
        self.assertEqual(len(self.state()['sessions']), 3)
        self.assertAllStopped()

    def test_wait_failure_with_id_leaks_nothing(self):
        rc, out = self.go(leak_at=2)
        self.assertEqual(rc, 1)
        self.assertIn('after creating session', out)
        self.assertAllStopped()

    def test_wait_failure_without_id_reconciled_by_key(self):
        rc, out = self.go(leak_at=0, leak_with_id=False)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.state()['sessions']), 1)
        self.assertAllStopped()

    def test_every_ambiguous_start_recovers_using_only_original_key_and_request(self):
        for knob in ('accepted_nonjson', 'accepted_502', 'initial_missing_id', 'initial_bad_id'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertAllStopped()
                requests = self.state()['start_requests']
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[1], requests[0] + ['--no-wait'])
                self.assertEqual(len(self.state()['sessions']), 1)
                self.assertNotIn('cleanup uncertain', out)

    def test_final_cleanup_retries_one_failed_recovery_receipt(self):
        rc, out = self.go(accepted_nonjson=True, replay_failure_count=1)
        self.assertEqual(rc, 1)
        self.assertAllStopped()
        requests = self.state()['start_requests']
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[1], requests[2])
        self.assertEqual(requests[1], requests[0] + ['--no-wait'])
        self.assertNotIn('cleanup uncertain', out)

    def test_unrecoverable_receipts_report_uncertainty_and_original_key(self):
        for config in ({'replay_failure_count': 2}, {'bad_recovered_id': True}):
            with self.subTest(config=config):
                rc, out = self.go(accepted_nonjson=True, **config)
                self.assertEqual(rc, 1)
                summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
                record = summary['starts'][0]
                self.assertTrue(record['cleanup_uncertain'])
                self.assertIsNone(record.get('id'))
                self.assertIn('cleanup uncertain', out)
                self.assertIn('original key ' + record['start_key'], out)
                requests = self.state()['start_requests']
                self.assertEqual(len(requests), 3)
                self.assertEqual(requests[1], requests[2])
                self.assertEqual(requests[1], requests[0] + ['--no-wait'])
                self.assertEqual(len(self.state()['sessions']), 1)
                self.assertFalse(next(iter(self.state()['sessions'].values()))['stopped'])
                self.assertFalse(any(x.startswith('stop:') for x in self.state()['log']))

    def test_uncertain_later_receipt_preserves_cleanup_of_all_known_owned_sessions(self):
        rc, out = self.go(accepted_nonjson_at=2, replay_failure_count=2)
        self.assertEqual(rc, 1)
        sessions = list(self.state()['sessions'].values())
        self.assertEqual(len(sessions), 3)
        self.assertTrue(all(s['stopped'] for s in sessions[:2]))
        self.assertFalse(sessions[2]['stopped'])
        summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
        self.assertTrue(summary['starts'][2]['cleanup_uncertain'])
        self.assertEqual(len(self.state()['start_requests']), 5)

    def test_blocking_start_hits_deadline_and_is_cleaned_up(self):
        with mock.patch.object(acc, 'RUNNING_BUDGET_S', 1):
            t0 = time.monotonic()
            rc, out = self.go(hang_start_at=0)
            self.assertLess(time.monotonic() - t0, 25)
        self.assertEqual(rc, 1)
        self.assertIn('timeout', out)
        self.assertAllStopped()

    def test_astra_picked(self):
        rc, out = self.go(models=['astra-1'] + OK_MODELS[1:])
        self.assertEqual(rc, 1)
        self.assertIn('astra picked', out)

    def test_luna_over_three(self):
        # Luna is a Hydrogen-engine draw, so a live run ends at its unavailable native proof; the quota rule is
        # still enforced by the summary over whatever records exist.
        recs = [{'n': i + 1, 'model': m, 'engine': 'antigravity' if m in (LUNA, MUSE) else 'claude', 'errors': []}
                for i, m in enumerate([LUNA] * 4 + [SOL, GLM, MUSE, SOL, GLM, MUSE])]
        self.assertIn('luna 4 > 3', acc.summarize(recs, [])['problems'])
        rc, out = self.go(models=[LUNA] * 4 + [SOL, GLM, MUSE, SOL, GLM, MUSE])
        self.assertEqual(rc, 1)
        self.assertIn('UNKNOWN_NATIVE_FRAMING', out)
        self.assertAllStopped()

    def test_luna_on_degraded_pool(self):
        one = [next(x for x in NATIVE_POOL if x['model'] == LUNA)]
        one[0] = dict(one[0], share=1.0)
        rc, out = self.go(pool=one, models=[LUNA] * 10)
        self.assertEqual(rc, 1)
        self.assertIn('luna picked from a degraded one-model pool', out)

    def test_too_few_distinct_models(self):
        rc, out = self.go(models=[SOL, GLM] * 5)
        self.assertEqual(rc, 1)
        self.assertIn('2 distinct models < 3', out)

    def test_missing_or_empty_pool_fails(self):
        rc, out = self.go(pool=[{'policy_id': 'p', 'model': 'sol', 'share': 0}])
        self.assertEqual(rc, 1)
        self.assertIn('invalid or zero share', out)
        rc, out = self.go(missing_picker=True)
        self.assertEqual(rc, 1)
        self.assertIn('picker evidence missing', out)

    def test_model_not_in_pool_shares(self):
        rc, out = self.go(models=['unlisted'] * 10)
        self.assertEqual(rc, 1)
        self.assertIn('not in the picker pool shares', out)

    def test_missing_host_and_workspace(self):
        rc, out = self.go(list_no_host=True)
        self.assertIn('host None != hetzner-aplexer', out)
        rc, out = self.go(list_no_workspace=True)
        self.assertIn('workspace path missing', out)
        self.assertEqual(rc, 1)
        rc, out = self.go(host='other-host')
        self.assertIn("host 'other-host'", out)

    def test_flat_show_is_rejected(self):
        rc, out = self.go(flat_show=True)
        self.assertEqual(rc, 1)
        self.assertIn('show response is not', out)

    def test_capture_prompt_only(self):
        rc, out = self.go(capture_prompt_only=True)
        self.assertEqual(rc, 1)
        self.assertIn('did not prove completed work', out)

    def test_stop_not_confirmed(self):
        rc, out = self.go(stop_not_confirmed=True)
        self.assertEqual(rc, 1)
        self.assertIn('stop_confirmed != true', out)

    def test_native_lingers_after_stop_but_exited_rows_do_not_count(self):
        with mock.patch.object(acc, 'STOP_BUDGET_S', 4):
            rc, out = self.go(native_lingers=True, fail_start_at=1)
        self.assertEqual(rc, 1)
        self.assertIn('still listed', out)
        rc, out = self.go(a_list_includes_exited=True)
        self.assertEqual(rc, 0, out)

    def test_alert_during_run(self):
        rc, out = self.go(alert_during=True)
        self.assertEqual(rc, 1)
        self.assertIn('dev alert occurrences during run: 1', out)
        self.assertNotIn('ACCEPTANCE PASS', out)

    def test_transient_resolved_and_deduplicated_alert_occurrences_fail(self):
        for knob in ('alert_transient', 'alert_dedup'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1)
                self.assertIn('dev alert occurrences during run: 1', out)
                self.assertAllStopped()
                summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
                self.assertEqual(summary['alert_watermarks']['before']['count'], 40)
                self.assertEqual(summary['alert_watermarks']['after']['count'], 41)

    def test_occurrence_contract_fails_closed_on_missing_active_only_or_stale_source(self):
        for knob in ('active_ids_only', 'alert_stale', 'alert_missing_metadata', 'alert_wrong_recipient'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1)
                self.assertIn('alert baseline unavailable', out)
                self.assertEqual(self.state()['sessions'], {})
        for knob in ('alert_reset', 'alert_generation_change', 'alert_post_stale'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1)
                self.assertTrue('alert watermark' in out, out)
                self.assertAllStopped()

    def test_fresh_cached_post_watermark_must_cover_end_of_run(self):
        rc, out = self.go(alert_cached_post=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('post-run alert watermark predates end-of-run cutoff', out)
        self.assertAllStopped()
        summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
        evidence_wm = summary['alert_watermarks']
        before, after = evidence_wm['before'], evidence_wm['after']
        self.assertEqual(after['generation'], before['generation'])
        self.assertEqual(after['count'], before['count'])
        self.assertGreater(after['observed_at_ms'], before['observed_at_ms'])
        self.assertLess(after['observed_at_ms'], evidence_wm['required_post_observed_at_ms'])
        self.assertLess(evidence_wm['required_post_observed_at_ms'] - after['observed_at_ms'], acc.ALERT_SAMPLE_MAX_AGE_MS)
        self.assertNotIn('stale or from the future', out)
        self.assertNotIn('reset or observation time regressed', out)

    def test_historical_occurrences_do_not_count_but_broken_sampler_fails_closed(self):
        rc, out = self.go(alert_count_before=40)
        self.assertEqual(rc, 0, out)
        rc, out = self.go(alerts_broken=True)
        self.assertEqual(rc, 1)
        self.assertIn('alert baseline unavailable', out)
        self.assertEqual(self.state()['sessions'], {})


class V2NativeAndLineageTests(Harness):
    """V1+V2 interactive native-pane, challenge/artifact, and owned-lineage cleanup regressions."""

    def test_plan_mode_renders_argv_without_invoking_hub(self):
        out_dir = self.tmp / 'plan-out'
        buf = io.StringIO()
        with mock.patch('sys.stdout', buf):
            rc = acc.main([
                '--plan',
                '--evidence', native_evidence(),
                '--commit', COMMIT,
                '--workspace', str(self.base_ws),
                '--intake-task-id', PARENT_INTAKE,
                '--output-dir', str(out_dir),
            ])
        self.assertEqual(rc, 0, buf.getvalue())
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data['plan']), 10)
        self.assertIn('--interactive', data['plan'][0]['start_argv'])
        self.assertNotIn('--prompt', data['plan'][0]['start_argv'])
        self.assertEqual(self.state()['sessions'], {})

    def test_cli_parser_rejects_interactive_with_prompt_and_accepts_new_start_argv(self):
        import argparse
        from agent_hub_host import session_control
        parser = argparse.ArgumentParser()
        session_control.add_commands(parser.add_subparsers(dest='cmd'))
        argv = acc.build_start_argv('/abs/base', PARENT_INTAKE, 'key-1')
        parsed = parser.parse_args(argv)
        self.assertTrue(parsed.interactive)
        self.assertIsNone(parsed.prompt)
        with self.assertRaises(SystemExit):
            parser.parse_args(argv + ['--prompt', 'bad'])

    def test_create_lost_response_replays_same_key_and_ten_unique_children(self):
        rc, out = self.go(create_fail_after_accept_at=0)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.state()['intakes']), 10)
        rc2, out2 = self.go(reuse_intake_id=True)
        self.assertEqual(rc2, 1, out2)
        self.assertIn('child intake id reused', out2)

    def test_headless_mode_and_legacy_headless_evidence_rejected(self):
        rc, out = self.go(force_headless_mode=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('session is not interactive', out)
        rc, out = self.go(evidence_bundle=evidence())
        self.assertEqual(rc, 1, out)
        self.assertIn('operator-validated same-commit receipt references required', out)
        self.assertEqual(self.state()['sessions'], {})

    def test_natural_exit_after_proof_before_live_stop_fails_sample(self):
        rc, out = self.go(natural_exit_after_proof=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('natural exit after proof before live STOP', out)
        self.assertAllStopped()

    def test_null_native_id_still_issues_standard_stop(self):
        rc, out = self.go(null_native_stop=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('no canonical native UUID', out)
        self.assertTrue(any(x.startswith('stop:') for x in self.state()['log']))
        self.assertAllStopped()

    def test_proven_continuation_and_verification_descendants_drained_not_counted(self):
        for knob in ('continuation_descendant', 'verification_descendant', 'fresh_successor_after_first_scan'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 0, out)
                self.assertAllStopped()
                summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
                self.assertEqual(len(summary['starts']), 10)
                self.assertTrue(summary['starts'][0]['cleanup_lineage'])

    def test_foreign_incomplete_archive_snapshot_change_over_limit_and_cas_conflict(self):
        for knob, expected in [
            ('foreign_assignment', 'foreign, missing or unprovable current session'),
            ('foreign_assigned_machine', "current assigned_machine_id 'foreign-host'"),
            ('pending_dispatch_state', 'non-terminal dispatch state'),
            ('history_seq_gap', 'intake history sequence gap'),
            ('history_incomplete_archive', 'history_archive has 3 unread archived events'),
            ('history_snapshot_changed', 'intake snapshot changed during history scan'),
            ('descendant_over_limit', 'exceeded 8 descendant bound'),
            ('archive_cas_conflict', 'archive CAS failed'),
        ]:
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn(expected, out)

    def test_trust_menu_and_negative_pane_renderings_fail(self):
        for knob in ('trust_menu_rendered', 'hydrogen_prose_command_exited', 'claude_waiting_only',
                     'claude_prose_only', 'claude_unterminated_echo', 'hydrogen_unrelated_action',
                     'hydrogen_unrelated_python', 'hydrogen_unrelated_shell', 'hydrogen_assistant_footer',
                     'hydrogen_truncated_header'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn('did not prove completed work', out)
                self.assertAllStopped()

    def test_ac_normalization_and_provenance_match_hub2_contract(self):
        _, ac = acc.make_child_brief_and_ac(PARENT_INTAKE, 1, PARENT_INTAKE, '/tmp/ch.bin', '/tmp/helper-1.py')
        norm = acc.normalize_acceptance_criteria(ac)
        self.assertEqual(len(norm), 2)
        self.assertFalse(any(line.startswith('1.') or line.startswith('2.') for line in norm))
        rc, out = self.go()
        self.assertEqual(rc, 0, out)
        for intake in self.state()['intakes'].values():
            self.assertIsNone(intake['parent_intake_id'])
            self.assertIn('provenance', intake)
            self.assertEqual(len(intake['acceptance_criteria']), 2)
            self.assertFalse(any(line.startswith('1.') or line.startswith('2.') for line in intake['acceptance_criteria']))
            self.assertEqual(intake['acceptance_criteria'][1], norm[1])

    def test_v2_artifact_tamper_and_link_negatives_fail(self):
        for knob in ('result_symlink', 'result_hardlink', 'result_duplicate_key', 'result_non_canonical', 'result_bad_digest', 'result_preexisting'):
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn('did not prove completed work', out)
                self.assertAllStopped()

    def test_b3_missing_or_drifted_current_custody_never_confirms_or_archives(self):
        for knob, expected in [
            ('cleanup_missing_dispatch', 'missing or malformed current dispatch'),
            ('cleanup_null_machine', 'current assigned_machine_id None'),
            ('cleanup_dispatch_missing_op', 'operation_id None is not a base UUID'),
            ('cleanup_dispatch_null_machine', 'machine_id UNKNOWN'),
            ('cleanup_epoch_drift', 'epoch drift 2 != 1'),
            ('cleanup_native_drift', 'native_id drift'),
            ('cleanup_stop_view_op_drift', 'operation_id drift'),
            ('archive_unproven', 'archive unproven'),
        ]:
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn(expected, out)
                self.assertAllStopped()
                report = json.loads(out[out.index('\n{\n') + 1:out.rindex('\n}') + 2])
                first = report['starts'][0]
                self.assertNotEqual(first.get('cleanup_status'), 'CONFIRMED')
                if knob != 'archive_unproven':
                    self.assertNotIn('intake:update', self.state()['log'])
                    self.assertFalse(first.get('archived'))
                else:
                    self.assertEqual(first.get('cleanup_status'), 'ARCHIVE_PENDING')

    def test_b3_lost_archive_ack_is_confirmed_only_by_final_recheck(self):
        rc, out = self.go(archive_lost_ack=True)
        report = json.loads(out[out.index('\n{\n') + 1:out.rindex('\n}') + 2])
        for rec in report['starts']:
            self.assertEqual(rec.get('cleanup_status'), 'CONFIRMED', out)
            self.assertTrue(rec.get('archived'))
        self.assertEqual(rc, 0, out)

    @staticmethod
    def real_shapes(attempt=0):
        """Sanitized copies of Root's genuine observation-1 field SHAPES (values synthetic)."""
        base, sid, native = '2ff7f80c-0d8b-58a7-a7ad-d24786513cdd', '743973dd-533c-55ab-a5fb-29c3b7a93125', '4a082cb8-f506-46fc-9beb-8ca53ccc10d2'
        cid, att = '65b8e3e4-c77a-4bdb-b937-a1c590297df6', '2ff7f80c-0d8b-58a7-a7ad-d24786513cdd:%d' % attempt
        live = {'id': sid, 'state': 'running', 'intake_id': cid, 'project_id': acc.PROJECT, 'machine_id': acc.HOST,
                'epoch': 1445, 'operation_id': att, 'native_id': native, 'host': acc.HOST, 'machine': acc.HOST,
                'mode': 'interactive', 'interactive': True, 'evidence': None, 'attention': None}
        exit_ev = {'operation_id': base, 'attempt_operation_id': att, 'session_id': sid, 'intake_id': cid,
                   'machine_id': acc.HOST, 'epoch': 1445, 'native_id': native, 'state': 'stopped', 'exit_code': 0,
                   'native_exit_confirmed': True, 'evidence': {'containment_empty': True, 'source': 'aplexer_retirement'}}
        stopped = dict(live, state='stopped', evidence=dict(exit_ev), stop_confirmed=True)
        dispatch = {'id': sid, 'operation_id': base, 'attempt_operation_id': att, 'intake_id': cid, 'machine_id': acc.HOST,
                    'epoch': 1445, 'state': 'failed', 'native_id': native, 'attempt': attempt, 'next_action_at': None}
        final = {'stopped': True, 'reaped': False, 'operation_id': base, 'attempt_operation_id': att, 'session_id': sid,
                 'machine_id': acc.HOST, 'epoch': 1445, 'reason': 'stop_confirmed', 'native_id': native,
                 'completion': {'controlled': True, 'session_id': sid, 'outcome': 'unfinished',
                                'reason': 'session_stop_confirmed', 'depth': 0},
                 'finish_after_stop': dict(exit_ev)}
        return live, stopped, dispatch, final, cid

    def test_b3_real_machine_vs_project_shapes_bind_base_and_attempt(self):
        for attempt in (0, 1):
            live, stopped, dispatch, final, cid = self.real_shapes(attempt)
            self.assertNotIn('attempt_operation_id', live)
            ident = acc._owned_identity(live, live['native_id'])
            self.assertEqual(ident['operation_id'], dispatch['operation_id'])
            self.assertEqual(ident['attempt_operation_id'], live['operation_id'])
            self.assertEqual(ident['attempt'], attempt)
            self.assertIsNone(acc._stop_view_problem(ident, stopped, 'stop view'))
            self.assertIsNone(acc._bind_view(ident, live, acc.machine_session_identity, 'pre-stop', acc.IDENTITY_KEYS))
            self.assertIsNone(acc._bind_view(ident, dispatch, acc.dispatch_identity, 'dispatch', acc.IDENTITY_KEYS))
            self.assertIsNone(acc.final_stop_proof_problem(final, ident, cid))
            # Raw operation_id labels differ across endpoints by contract; they are never compared directly.
            self.assertNotEqual(live['operation_id'], dispatch['operation_id'])
            # An optional redundant Machine alias is accepted only when it agrees.
            self.assertIsNone(acc._stop_view_problem(ident, dict(stopped, attempt_operation_id=live['operation_id']), 'v'))

    def test_b3_contradictory_malformed_missing_or_different_tuple_fails(self):
        live, stopped, dispatch, final, cid = self.real_shapes(0)
        ident = acc._owned_identity(live, live['native_id'])
        base = dispatch['operation_id']
        other = 'bbbbbbbb-0d8b-58a7-a7ad-d24786513cdd'
        for bad in ['opaque-op', base, base.upper() + ':0', base + ':01', base + ':-1', base + ':0:0', None, 7,
                    'prefix-' + base + ':0']:
            with self.subTest(machine_op=bad):
                with self.assertRaises(acc.IdentityError):
                    acc.machine_session_identity(dict(live, operation_id=bad))
                self.assertIsNone(acc._owned_identity(dict(live, operation_id=bad), live['native_id'])['operation_id'])
        machine_neg = [
            (dict(stopped, attempt_operation_id=base), 'contradicts Machine operation_id'),
            (dict(stopped, attempt=1), 'contradicts Machine operation_id'),
            (dict(stopped, attempt=True), 'contradicts Machine operation_id'),
            (dict(stopped, operation_id=other + ':0'), 'operation_id drift'),
            (dict(stopped, operation_id=base + ':1'), 'drift'),
            (dict(stopped, epoch=1446), 'epoch drift'),
            (dict(stopped, epoch=None), 'epoch UNKNOWN'),
            (dict(stopped, machine_id='other-host', host='other-host', machine='other-host'), 'machine_id drift'),
            (dict(stopped, host='other-host'), 'contradicts machine_id'),
            (dict(stopped, native_id='99999999-0000-4000-8000-000000000000'), 'native_id drift'),
            (dict(stopped, id='00000000-0000-4000-8000-000000000001'), 'session_id drift'),
            (dict(stopped, evidence=dict(stopped['evidence'], attempt_operation_id=base + ':1')), 'session exit evidence'),
            (dict(stopped, evidence=dict(stopped['evidence'], operation_id=other, attempt_operation_id=other + ':0')), 'session exit evidence'),
        ]
        for view, msg in machine_neg:
            with self.subTest(msg=msg):
                problem = acc._stop_view_problem(ident, view, 'stop view')
                self.assertIsNotNone(problem)
                self.assertIn(msg, problem)
        dispatch_neg = [
            (None, 'missing or malformed'),
            ('unknown', 'missing or malformed'),
            (dict(dispatch, operation_id=dispatch['attempt_operation_id']), 'is not a base UUID'),
            ({k: v for k, v in dispatch.items() if k != 'attempt_operation_id'}, 'not strict'),
            (dict(dispatch, attempt_operation_id=other + ':0'), 'attempt_operation_id base'),
            (dict(dispatch, attempt=1), 'contradicts attempt_operation_id'),
            (dict(dispatch, attempt=False), 'contradicts attempt_operation_id'),
            (dict(dispatch, operation_id=other, attempt_operation_id=other + ':0'), 'operation_id drift'),
            (dict(dispatch, epoch=1446), 'epoch drift'),
            (dict(dispatch, machine_id=None), 'machine_id UNKNOWN'),
            (dict(dispatch, native_id=None), 'native_id UNKNOWN'),
            (dict(dispatch, id='00000000-0000-4000-8000-000000000001'), 'session_id drift'),
        ]
        for view, msg in dispatch_neg:
            with self.subTest(dispatch=msg):
                problem = acc._bind_view(ident, view, acc.dispatch_identity, 'dispatch', acc.IDENTITY_KEYS)
                self.assertIsNotNone(problem)
                self.assertIn(msg, problem)
        fas = final['finish_after_stop']
        final_neg = [
            (None, 'missing'),
            (dict(final, attempt_operation_id=base + ':1'), 'drift'),
            (dict(final, finish_after_stop=dict(fas, attempt_operation_id=base + ':1')), 'finish_after_stop'),
            (dict(final, finish_after_stop=dict(fas, operation_id=base + ':0')), 'not a base UUID'),
            (dict(final, finish_after_stop=None), 'finish_after_stop'),
            (dict(final, reason='finished'), 'not a confirmed stop'),
            (dict(final, completion=dict(final['completion'], reason='session_finished')), 'session_stop_confirmed'),
            (dict(final, finish_after_stop=dict(fas, native_exit_confirmed=False)), 'native exit'),
            (dict(final, finish_after_stop=dict(fas, evidence={'containment_empty': False})), 'empty containment'),
            (dict(final, finish_after_stop=dict(fas, intake_id='other')), 'finish_after_stop'),
            (dict(final, epoch=1446), 'epoch drift'),
        ]
        for view, msg in final_neg:
            with self.subTest(final=msg):
                problem = acc.final_stop_proof_problem(view, ident, cid)
                self.assertIsNotNone(problem)
                self.assertIn(msg, problem)
        # NULL native original: never equality, even when later views carry a native id.
        null_ident = acc._owned_identity(live, None)
        self.assertIn('native_id UNKNOWN', acc._bind_view(null_ident, dispatch, acc.dispatch_identity, 'd', acc.IDENTITY_KEYS))
        self.assertIn('native_id UNKNOWN', acc.final_stop_proof_problem(final, null_ident, cid))

    def test_b3_process_fake_real_shapes_end_to_end(self):
        rc, out = self.go(show_redundant_alias=True)
        self.assertEqual(rc, 0, out)
        self.assertAllStopped()
        report = json.loads(out[out.index('\n{\n') + 1:out.rindex('\n}') + 2])
        for rec in report['starts']:
            self.assertRegex(rec['attempt_operation_id'], r'^%s:0$' % re.escape(rec['operation_id']))
            self.assertEqual(rec['attempt'], 0)
        for intake in self.state()['intakes'].values():
            self.assertEqual(intake['status'], 'archived')
            self.assertEqual(intake['dispatch']['state'], 'failed')
            self.assertEqual(intake['final_evidence']['completion']['reason'], 'session_stop_confirmed')
        for knob, msg, archived in [
            ('show_alias_contradiction', 'contradicts Machine operation_id', False),
            ('show_attempt_contradiction', 'contradicts Machine operation_id', False),
            ('show_malformed_operation', 'not strict <uuid>:<attempt>', False),
            ('start_response_base_drift', 'start response operation_id drift', None),
            ('stop_evidence_contradiction', 'session exit evidence', False),
            ('dispatch_alias_base_mismatch', 'attempt_operation_id base', False),
            ('dispatch_attempt_int_contradiction', 'contradicts attempt_operation_id', False),
            ('dispatch_machine_attempt_as_base', 'is not a base UUID', False),
            ('dispatch_missing_alias', 'not strict', False),
            ('final_missing', 'final_evidence missing', False),
            ('final_attempt_contradiction', 'finish_after_stop', False),
            ('final_reason_not_stop', 'session_stop_confirmed', False),
            ('final_no_native_exit', 'native exit', False),
            ('final_containment_not_empty', 'empty containment', False),
        ]:
            with self.subTest(knob=knob):
                rc, out = self.go(**{knob: True})
                self.assertEqual(rc, 1, out)
                self.assertIn(msg, out)
                self.assertAllStopped()
                stops = [x for x in self.state()['log'] if x.startswith('stop:')]
                self.assertTrue(stops)
                if archived is False:
                    self.assertNotIn('intake:update', self.state()['log'])
                    self.assertFalse(any(i['status'] == 'archived' for i in self.state()['intakes'].values()))

    def test_b3_pinned_review_counterexample_null_dispatch_is_unknown(self):
        run, sid, cid = (PARENT_INTAKE, '22222222-3333-4444-8555-666666666666', '33333333-4444-4555-8666-777777777777')
        base, native = 'abcdef01-2345-5678-89ab-cdef01234567', '55555555-6666-4777-8888-999999999999'
        show = {'id': cid, 'project': acc.PROJECT, 'session_id': sid, 'assigned_machine_id': acc.HOST, 'updated_at': 100,
                'status': 'open', 'dispatch': None, 'final_evidence': None}

        class R:
            calls = []

            def json(self, tool, args, timeout):
                self.calls.append(args)
                return {'intake': dict(show, status='archived')}
        # Machine-shaped stop view: operation_id IS the attempt id, no redundant alias.
        stop_view = {'id': sid, 'state': 'stopped', 'stop_confirmed': True, 'operation_id': base + ':0',
                     'epoch': 1, 'machine_id': acc.HOST, 'native_id': native}
        owned = {'session_id': sid, 'operation_id': base, 'attempt_operation_id': base + ':0', 'attempt': 0,
                 'epoch': 1, 'machine_id': acc.HOST, 'native_id': native}
        for owned_extra, view, msg in [
            ({}, stop_view, 'missing or malformed current dispatch'),
            ({'attempt_operation_id': None}, stop_view, 'attempt_operation_id UNKNOWN'),
            ({}, dict(stop_view, epoch=None), 'epoch UNKNOWN'),
            ({}, dict(stop_view, operation_id='bbbbbbbb-2345-5678-89ab-cdef01234567:0'), 'operation_id drift'),
            ({}, dict(stop_view, operation_id='original-op'), 'identity invalid'),
            ({}, dict(stop_view, attempt_operation_id=base), 'contradicts Machine operation_id'),
        ]:
            with self.subTest(msg=msg):
                r = R(); r.calls = []
                rec = {'id': sid, 'child_intake_id': cid, 'owned_identity': dict(owned, **owned_extra), 'host': acc.HOST,
                       'native_id': native, 'stop_key': 'synthetic-stop', 'errors': [], 'n': 1}
                with mock.patch.object(acc, '_stop_single_session', lambda *a, v=view: (dict(v), None)), \
                        mock.patch.object(acc, 'scan_intake_lineage',
                                          lambda *a: ({'show': show, 'events': [{'seq': 1, 'kind': 'created', 'body': {}}]}, None)):
                    acc.stop_and_drain_lineage(r, rec, run, lambda: 0, lambda _: None)
                self.assertFalse(rec['cleanup_confirmed'])
                self.assertEqual(rec['cleanup_status'], 'UNKNOWN')
                self.assertFalse(rec.get('archived'))
                self.assertEqual(r.calls, [])
                self.assertTrue(any(msg in e for e in rec['errors']), rec['errors'])

    def test_b1_actual_project_create_shape_and_no_invented_source(self):
        brief, ac = acc.make_child_brief_and_ac(PARENT_INTAKE, 1, PARENT_INTAKE, '/tmp/ch.bin', '/tmp/helper-1.py')
        cid = '33333333-4444-4555-8666-777777777777'
        title = 'Phase7 owned run %s/%d' % (PARENT_INTAKE, 1)
        # Mirrors hub2/src/project-do.ts create INSERT + viewIntake (no saga yet => no dispatch key).
        actual = {'id': cid, 'project': acc.PROJECT, 'title': title, 'text': brief,
                  'acceptance_criteria': acc.normalize_acceptance_criteria(ac), 'status': 'open',
                  'assigned_machine_id': None, 'session_id': None, 'linked_sessions': [], 'priority': 2,
                  'original_ask': {'title': title, 'text': brief}, 'provenance': {'type': 'user', 'code_task': True},
                  'parent_intake_id': None, 'schedule_id': None, 'occurrence_id': None, 'final_evidence': None,
                  'created_at': 1, 'updated_at': 1}

        class R:
            def __init__(self, view):
                self.view, self.calls = view, []

            def json(self, tool, args, timeout):
                self.calls.append(args)
                return {'intake': self.view} if args[1] in ('create', 'show') else {'ok': True}
        r = R(actual)
        rec = {'create_key': 'k-create', 'start_key': 'k-start'}
        self.assertEqual(acc.create_child_intake(r, rec, PARENT_INTAKE, 1, PARENT_INTAKE, 'b', 'a', brief, ac, set(), lambda: 0), cid)
        self.assertFalse(any(flag.startswith('--source') for flag in r.calls[0]))
        receipt = rec['create_receipt']
        self.assertEqual(receipt['provenance'], {'type': 'user', 'code_task': True})
        self.assertFalse(receipt['structural_parent_link'])
        self.assertEqual(receipt['text_sha256'], hashlib.sha256(brief.encode()).hexdigest())
        note = r.calls[-1]
        self.assertEqual(note[:5], ['intake', 'append', PARENT_INTAKE, '--key', 'acc10-own-%s-1' % PARENT_INTAKE])
        self.assertIn('create_key=k-create', note[-1])
        self.assertIn(receipt['acceptance_criteria_sha256'], note[-1])
        for change, msg in [
            ({'provenance': {'type': 'forwarded', 'reference': 'parent:x'}}, 'provenance mismatch'),
            ({'provenance': {'type': 'user', 'code_task': False}}, 'provenance mismatch'),
            ({'parent_intake_id': PARENT_INTAKE}, 'structural parent_intake_id'),
            ({'text': brief + ' '}, 'text mismatch'),
            ({'original_ask': {'title': title, 'text': 'x'}}, 'original_ask mismatch'),
            ({'acceptance_criteria': ac.splitlines()}, 'acceptance_criteria mismatch'),
            ({'session_id': '22222222-3333-4444-8555-666666666666'}, 'not open/unassigned'),
        ]:
            with self.subTest(msg=msg):
                with self.assertRaisesRegex(RuntimeError, msg):
                    acc.create_child_intake(R(dict(actual, **change)), dict(rec), PARENT_INTAKE, 1, PARENT_INTAKE,
                                            'b', 'a', brief, ac, set(), lambda: 0)

    def test_b1_fake_legacy_forwarded_provenance_is_rejected(self):
        rc, out = self.go(legacy_forwarded_provenance=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('provenance mismatch', out)

    def test_b2_plain_pane_and_label_never_attest_antigravity(self):
        """A class label is not attestation: no plain-pane rendering or Claude-shaped record can prove antigravity."""
        run = '40a49778-f092-450d-a042-12ad276ab134'
        cmd = acc.probe_command(run, 3)
        self.assertRegex(cmd, r'^python3 \.local/h7_[0-9a-f]{12}_03\.py$')
        self.assertEqual(cmd, 'python3 ' + acc.hydrogen_frame.h7_rel_path(run, 3))
        expected = 'AC10_WORK_V2 %s 3 %s %s 5050' % (run, 'a' * 64, 'b' * 40)
        brief, _ = acc.make_child_brief_and_ac(run, 3, PARENT_INTAKE, '/tmp/ch.bin', '/tmp/helper-3.py')
        self.assertIn('`%s`' % cmd, brief)
        self.assertIn('`/tmp/helper-3.py`', brief)
        self.assertNotIn(expected, brief)
        row = '● Ran (%s) (ctrl+o to expand)\n' % cmd
        panes = {
            'exact_row_alone': row + '> ',
            'assistant_footer_exact_row': 'Assistant readiness summary:\n' + row + 'I am done.\n',
            'row_plus_expected_stdout': row + '  ' + expected + '\n',
            'claude_shaped_block': '● Bash(%s)\n  ⎿  %s\n❯ ' % (cmd, expected),
        }
        labels = ['native_exact_probe_header_plus_artifact_v3', 'native_action_plus_artifact_v2',
                  acc.NATIVE_PROOF_CLASSES['claude'], acc.NATIVE_PROOF_CLASSES['antigravity']]
        for label in labels:
            t = dict(native_transport('antigravity', COMMIT), proof_class=label)
            self.assertFalse(acc.native_transport_ok('antigravity', t, COMMIT), label)
            for name, pane in panes.items():
                with self.subTest(label=label, pane=name):
                    self.assertFalse(acc.native_pane_output_ok(pane, 'antigravity', t, expected, brief, command_id=cmd))
        self.assertFalse(hasattr(acc, 'hydrogen_framing_diagnostic'))
        # A Claude-shaped (plain-pane) antigravity record in an operator bundle is refused.
        path = Path(native_evidence())
        ev = json.loads(path.read_text())
        ev['operator_validation']['capture_transports']['antigravity'] = native_transport('antigravity', COMMIT)
        problems = acc.validate_native_evidence(ev, str(path), COMMIT)
        self.assertIn('verified interactive native-pane capture transport records required', problems)

    def test_b2_hydrogen_draw_fails_unavailable_with_artifact_only_and_cleanup(self):
        for knob, artifact in [({}, 'VALIDATED'), ({'no_probe_file': True}, 'absent')]:
            with self.subTest(knob=knob):
                rc, out = self.go(models=[MUSE], **knob)
                self.assertEqual(rc, 1, out)
                self.assertIn('native proof UNAVAILABLE for engine antigravity: UNKNOWN_NATIVE_FRAMING', out)
                self.assertIn('artifact-only=%s' % artifact, out)
                self.assertNotIn('ACCEPTANCE PASS', out)
                self.assertAllStopped()
                report = json.loads(out[out.index('\n{\n') + 1:out.rindex('\n}') + 2])
                first = report['starts'][0]
                self.assertIs(first.get('capture_verified'), False)
                self.assertEqual(first['native_proof']['root_checker_status'], 'NEEDS_ROOT_RENDERER_ATTESTATION')
                self.assertIs(first['native_proof']['native_action_verified'], False)
                self.assertEqual(bool(first.get('artifact_only')), artifact == 'VALIDATED')
        with self.assertRaises(TimeoutError):
            acc.verify_work_artifact(None, str(self.tmp), PARENT_INTAKE, 1, PARENT_INTAKE, PARENT_INTAKE,
                                     PARENT_INTAKE, PARENT_INTAKE, b'x' * 32, lambda: 10, 5)

    def test_deadline_each_git_call_gets_fresh_remaining_budget(self):
        class Clock:
            t = 100.0

            def __call__(self):
                return self.t

        class R:
            def __init__(self, clock, costs):
                self.clock, self.costs, self.timeouts = clock, list(costs), []

            def _spend(self, timeout):
                self.timeouts.append(timeout)
                self.clock.t += self.costs.pop(0)

            def run(self, tool, args, timeout):
                self._spend(timeout)
                return 0, 'b' * 40 + '\n', ''

            def run_bytes(self, tool, args, timeout):
                self._spend(timeout)
                return 0, b'', b''
        # First call consumes most of the budget; the second gets only what remains (not the initial value).
        clock = Clock(); r = R(clock, [24.0, 1.0])
        acc.observe_git_state(r, '/ws', clock, 100.0 + 30.0)
        self.assertEqual(r.timeouts, [30.0, 6.0])
        clock = Clock(); r = R(clock, [10.0, 1.0])
        acc.observe_git_state(r, '/ws', clock, 100.0 + 90.0)
        self.assertEqual(r.timeouts, [30.0, 30.0])
        # First call exhausts the 90s total: the second Git call is never started.
        clock = Clock(); r = R(clock, [91.0, 1.0])
        with self.assertRaises(TimeoutError):
            acc.observe_git_state(r, '/ws', clock, 100.0 + 90.0)
        self.assertEqual(r.timeouts, [30.0])
        # Second call overruns: refused after it returns even though both commands "succeeded".
        clock = Clock(); r = R(clock, [5.0, 30.0])
        with self.assertRaises(TimeoutError):
            acc.observe_git_state(r, '/ws', clock, 100.0 + 30.0)
        self.assertEqual(r.timeouts, [30.0, 25.0])

    def test_previous_attempt_fallback_requires_full_binding(self):
        operation = '22222222-2222-4222-8222-222222222222'
        owned = {'session_id': PARENT_INTAKE, 'operation_id': operation,
                 'attempt_operation_id': operation + ':1', 'attempt': 1,
                 'epoch': 7, 'machine_id': acc.HOST}
        created = dict(owned, attempt=0, attempt_operation_id=operation + ':0')
        self.assertTrue(acc._previous_attempt_matches(owned, created, PARENT_INTAKE))
        for field, values in (('epoch', (None, 8)),
                              ('machine_id', (None, 'foreign-machine'))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertFalse(acc._previous_attempt_matches(
                        owned, dict(created, **{field: value}), PARENT_INTAKE))
        self.assertFalse(acc._previous_attempt_matches(owned, created, 'foreign-session'))
        self.assertFalse(acc._previous_attempt_matches(dict(owned, attempt=0), created, PARENT_INTAKE))

    def test_proof_budget_never_floors_positive_remaining_time(self):
        self.assertEqual(acc._fresh_git_timeout(lambda: 0, 0.001, 'test'), 0.001)
        for now in (0.001, 1):
            with self.subTest(now=now), self.assertRaises(TimeoutError):
                acc._fresh_git_timeout(lambda: now, 0.001, 'test')

    def test_artifact_deadline_is_checked_before_each_read(self):
        class Clock:
            t = 0.0
            def __call__(self):
                return self.t
        # A slow probe read must not begin a result read after the total deadline.
        clock = Clock()
        def probe_read(*args):
            clock.t = 91.0
            return b'probe body'
        with mock.patch.object(acc, 'validate_no_symlink_ancestors'), \
             mock.patch.object(acc, 'bounded_file', side_effect=probe_read) as read, \
             mock.patch.object(acc.os, 'lstat') as metadata:
            with self.assertRaises(TimeoutError):
                acc.verify_work_artifact(None, '/ws', PARENT_INTAKE, 1, PARENT_INTAKE,
                                         PARENT_INTAKE, PARENT_INTAKE, PARENT_INTAKE,
                                         b'x' * 32, clock, 90.0, pinned_probe=b'probe body')
            self.assertEqual(read.call_count, 1)
            metadata.assert_not_called()
        # Path validation may also exhaust the budget before either file is opened.
        for pinned in (None, b'probe body'):
            clock = Clock()
            def slow_path(*args):
                clock.t = 91.0
            with self.subTest(pinned=pinned), \
                 mock.patch.object(acc, 'validate_no_symlink_ancestors', side_effect=slow_path), \
                 mock.patch.object(acc, 'bounded_file') as read:
                with self.assertRaises(TimeoutError):
                    acc.verify_work_artifact(None, '/ws', PARENT_INTAKE, 1, PARENT_INTAKE,
                                             PARENT_INTAKE, PARENT_INTAKE, PARENT_INTAKE,
                                             b'x' * 32, clock, 90.0, pinned_probe=pinned)
                read.assert_not_called()
        # A result read crossing the deadline must not start a metadata read.
        clock = Clock()
        with mock.patch.object(acc, 'validate_no_symlink_ancestors'), \
             mock.patch.object(acc, 'bounded_file', side_effect=probe_read) as read, \
             mock.patch.object(acc.os, 'lstat') as metadata:
            with self.assertRaises(TimeoutError):
                acc.verify_work_artifact(None, '/ws', PARENT_INTAKE, 1, PARENT_INTAKE,
                                         PARENT_INTAKE, PARENT_INTAKE, PARENT_INTAKE,
                                         b'x' * 32, clock, 90.0)
            self.assertEqual(read.call_count, 1)
            metadata.assert_not_called()

    def test_porcelain_z_rename_copy_counting(self):
        self.assertEqual(acc.parse_porcelain_z_count(b''), 0)
        self.assertEqual(acc.parse_porcelain_z_count(b' M a.py\x00R  new.py\x00old.py\x00C  c.py\x00src.py\x00'), 3)
        with self.assertRaises(ValueError):
            acc.parse_porcelain_z_count(b' M a.py')
        with self.assertRaises(ValueError):
            acc.parse_porcelain_z_count(b'R  new.py\x00')


class ParserTests(unittest.TestCase):
    """Synthetic fixtures use the actual CLI response schema, not live success evidence."""

    def test_show_is_wrapped(self):
        sess = acc.session_of(fixture('hub_sessions_show.json'))
        self.assertEqual(sess['machine_id'], 'hetzner-aplexer')
        self.assertEqual(acc.row_host(sess), 'hetzner-aplexer')
        with self.assertRaises(RuntimeError):
            acc.session_of(sess)  # flat shape

    def test_list_rows_and_workspace_from_evidence(self):
        rows = acc.list_rows(fixture('hub_sessions_list.json'))
        self.assertEqual(acc.row_host(rows[0]), 'hetzner-aplexer')
        self.assertTrue(acc.row_workspace(rows[0]).startswith('/'))
        with self.assertRaises(RuntimeError):
            acc.list_rows({'nothing': 1})

    def test_pool_fixture_valid_and_bad_pools_rejected(self):
        self.assertEqual(acc.pool_problems(fixture('hub_sessions_pick.json')['pool']), [])
        for bad in (None, [], [{'share': 0}], [{'share': float('nan')}], [{'share': 0.2}, {'share': 0.2}]):
            self.assertTrue(acc.pool_problems(bad), bad)

    def test_live_natives_ignores_exited(self):
        rows = fixture('a_list_running.json')
        rows.append(dict(rows[0], id='gone', state='exited'))

        class R:
            def json(self, *a):
                return rows
        self.assertEqual(acc.live_natives(R(), 5), {rows[0]['id']})

    def test_evidence_fail_closed(self):
        for ev, commit, msg in [(None, 'abc', 'evidence'), (evidence(commit='zzz'), 'abc', '!='),
                                (evidence(canary='failure'), 'abc', 'not green'),
                                (evidence(), None, 'commit unknown'), ('/nonexistent', 'abc', 'unreadable')]:
            problems = acc.check_evidence(ev, commit)
            self.assertTrue(any(msg in p for p in problems), (msg, problems))
        self.assertEqual(acc.check_evidence(evidence(), COMMIT), [])

    def test_watermark_requires_fresh_strict_metadata_and_monotonic_observations(self):
        good = dict(kind='dev_alert_occurrences_v1', recipient='dev@dtcdev.click',
                    generation='synthetic-epoch', count=0, observed_at_ms=100000)
        class R:
            def __init__(self, data): self.data = data
            def json(self, *args): return self.data
        self.assertEqual(acc.sample_alerts(R(good), wall_clock=lambda: 100), good)
        for patch in ({'count': True}, {'count': -1}, {'count': 0.5}, {'generation': ''},
                      {'observed_at_ms': True}, {'observed_at_ms': 39000}, {'observed_at_ms': 105001}):
            with self.subTest(patch=patch), self.assertRaises(RuntimeError):
                acc.sample_alerts(R(dict(good, **patch)), wall_clock=lambda: 100)
        self.assertEqual(acc.alert_changes(good, good), [])
        self.assertTrue(acc.alert_changes(good, dict(good, observed_at_ms=99999)))

    def test_probe_ignores_prompt_echo(self):
        prompt, marker = acc.make_probe()
        self.assertNotIn(marker, prompt)
        for captured in (prompt, prompt + '\n' + marker):
            self.assertFalse(acc.probe_output_ok(captured, prompt, marker, 'codex', transport()))
        for event in [
            {'type': 'item.started', 'item': {'type': 'command_execution', 'aggregated_output': marker}},
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': marker}},
            {'type': 'reasoning', 'text': marker}, {'prompt': marker},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': marker}},
            {'type': 'result', 'result': marker},
            {'type': 'tool_use', 'part': {'type': 'tool', 'tool': 'bash', 'state': {'status': 'running', 'output': marker}}},
            {'type': 'tool_result', 'tool_use_id': 'synthetic-tool', 'content': marker},
        ]:
            for engine in acc.CAPTURE_FORMATS:
                self.assertFalse(acc.probe_output_ok(json.dumps(event), prompt, marker, engine, transport(engine)), event)

    def test_completed_tool_output_is_bound_to_verified_engine_format(self):
        prompt, marker = acc.make_probe()
        codex = {'type': 'item.completed', 'item': {'type': 'command_execution', 'status': 'completed', 'exit_code': 0, 'aggregated_output': marker}}
        go = {'type': 'tool_use', 'part': {'type': 'tool', 'tool': 'bash', 'state': {'status': 'completed', 'output': marker}}}
        for engine in ('codex', 'zai', 'go'):
            matching, foreign = (go, codex) if engine == 'go' else (codex, go)
            self.assertTrue(acc.probe_output_ok(json.dumps(matching), prompt, marker, engine, transport(engine)))
            self.assertFalse(acc.probe_output_ok(json.dumps(foreign), prompt, marker, engine, transport(engine)))
            self.assertFalse(acc.probe_output_ok(json.dumps(matching), prompt, marker))
            for bad in (None, True, {}, dict(transport(engine), engine='claude'),
                        dict(transport(engine), format='plaintext'), dict(transport(engine), machine_id='other'),
                        dict(transport(engine), host_release_sha='bad'), dict(transport(engine), provider_version=''),
                        dict(transport(engine), profile_set_sha256='bad')):
                self.assertFalse(acc.probe_output_ok(json.dumps(matching), prompt, marker, engine, bad), bad)
        for engine in ('claude', 'antigravity'):
            for event in (codex, go):
                forged = dict(transport('go'), engine=engine)
                self.assertFalse(acc.probe_output_ok(json.dumps(event), prompt, marker, engine, forged))
        for status in ('in_progress', 'failed', False):
            event = dict(codex, item=dict(codex['item'], status=status))
            self.assertFalse(acc.probe_output_ok(json.dumps(event), prompt, marker, 'codex', transport()))
        event = dict(codex, item=dict(codex['item'], exit_code=False))
        self.assertFalse(acc.probe_output_ok(json.dumps(event), prompt, marker, 'codex', transport()))

    def test_claude_and_argon_require_complete_pairs_in_one_capture_and_attestation(self):
        prompt, marker = acc.make_probe()
        for engine in ('claude', 'antigravity'):
            frames = provider_frames(engine, marker)
            with self.subTest(engine=engine):
                self.assertTrue(acc.probe_output_ok(jsonl(frames), prompt, marker, engine, transport(engine)))
                self.assertTrue(acc.probe_output_ok('clipped earlier frame\n' + jsonl(frames), prompt, marker, engine, transport(engine)))
                for text in (jsonl(frames[:1]), jsonl(frames[1:]), jsonl(frames)[:-2],
                             json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': jsonl(frames)}]}}),
                             jsonl(frames[:1]) + '\nmalformed middle\n' + jsonl(frames[1:])):
                    self.assertFalse(acc.probe_output_ok(text, prompt, marker, engine, transport(engine)), text)
                self.assertFalse(acc.probe_output_ok(jsonl(frames), prompt, marker, engine, None))
                for record in (dict(transport(engine), format='codex_jsonl_v1'), dict(transport(engine), provider_version=''),
                               dict(transport(engine), profile_set_sha256='bad')):
                    self.assertFalse(acc.probe_output_ok(jsonl(frames), prompt, marker, engine, record))

    def test_claude_native_empty_bash_placeholder_is_nonproof_only(self):
        import copy
        PLACEHOLDER = "(Bash completed with no output)"
        session = "11111111-2222-4333-8444-555555555555"
        marker = "SYNTHETIC-MARKER-0001"

        def pair(tool_id, command, content, detail, background=None, **result_overrides):
            params = {"command": command}
            if background is not None:
                params["run_in_background"] = background
            block = {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False}
            block.update(result_overrides)
            return [
                {"type": "assistant", "session_id": session, "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool_id, "name": "Bash", "input": params}]}},
                {"type": "user", "session_id": session, "message": {"role": "user", "content": [block]},
                 "tool_use_result": detail},
            ]

        def empty_detail(**changes):
            detail = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False}
            detail.update(changes)
            return detail

        def empty(tool_id="toolu_synthetic_empty", **kw):
            return pair(tool_id, "true", kw.pop("content", PLACEHOLDER), kw.pop("detail", empty_detail()), **kw)

        genuine = pair("toolu_synthetic_marker", "python3 synthetic_print.py", marker + "\n",
                       {"stdout": marker + "\n", "stderr": "", "interrupted": False})

        def text(frames):
            return "\n".join(json.dumps(f) for f in frames) + "\n"

        parsers = (acc.paired_stream_output_ok,)
        for parser in parsers:
            # Exact paired empty placeholder before or after a complete genuine pair is accepted.
            self.assertTrue(parser(text(empty() + genuine), marker, "claude"))
            self.assertTrue(parser(text(genuine + empty()), marker, "claude"))
            # The placeholder alone is NONPROOF: it never proves.
            self.assertFalse(parser(text(empty()), marker, "claude"))
            self.assertFalse(parser(text(empty() + empty("toolu_synthetic_empty_two")), marker, "claude"))
            # The marker appearing only in the command of an empty-result call is not proof.
            self.assertFalse(parser(text(pair("toolu_synthetic_echo", "echo " + marker + " > /dev/null", PLACEHOLDER, empty_detail())), marker, "claude"))

        def mutated(change):
            frames = empty()
            change(frames)
            return text(frames + genuine)

        def result(frames):
            return frames[1]["message"]["content"][0]

        negatives = {
            "trailing newline": lambda f: result(f).update(content=PLACEHOLDER + "\n"),
            "lowercase": lambda f: result(f).update(content=PLACEHOLDER.lower()),
            "trailing space": lambda f: result(f).update(content=PLACEHOLDER + " "),
            "unknown suffix": lambda f: result(f).update(content=PLACEHOLDER + " extra"),
            "marker suffix": lambda f: result(f).update(content=PLACEHOLDER + "\n" + marker),
            "fake json content": lambda f: result(f).update(content='{"type":"result","result":"' + marker + '"}'),
            "footer content": lambda f: result(f).update(content=PLACEHOLDER + "\nCommand exited with code 0."),
            "nonce content": lambda f: result(f).update(content=marker),
            "nonempty stdout": lambda f: f[1]["tool_use_result"].update(stdout="x"),
            "marker stdout": lambda f: f[1]["tool_use_result"].update(stdout=marker),
            "stderr": lambda f: f[1]["tool_use_result"].update(stderr="synthetic error"),
            "error flag": lambda f: result(f).update(is_error=True),
            "missing error flag": lambda f: result(f).pop("is_error"),
            "interrupted": lambda f: f[1]["tool_use_result"].update(interrupted=True),
            "missing interrupted": lambda f: f[1]["tool_use_result"].pop("interrupted"),
            "image": lambda f: f[1]["tool_use_result"].update(isImage=True),
            "missing image flag": lambda f: f[1]["tool_use_result"].pop("isImage"),
            "no output expected": lambda f: f[1]["tool_use_result"].update(noOutputExpected=True),
            "missing no-output flag": lambda f: f[1]["tool_use_result"].pop("noOutputExpected"),
            "background id": lambda f: f[1]["tool_use_result"].update(backgroundTaskId="bg_synth_01"),
            "wrong session": lambda f: f[1].update(session_id="99999999-2222-4333-8444-555555555555"),
            "wrong role": lambda f: f[1]["message"].update(role="assistant"),
            "missing detail": lambda f: f[1].pop("tool_use_result"),
            "non-object detail": lambda f: f[1].update(tool_use_result=PLACEHOLDER),
            "list content": lambda f: result(f).update(content=[{"type": "text", "text": PLACEHOLDER}]),
            "reused id with genuine": lambda f: (result(f).update(tool_use_id="toolu_synthetic_marker"),
                                                 f[0]["message"]["content"][0].update(id="toolu_synthetic_marker")),
            "duplicate result": lambda f: f.append(copy.deepcopy(f[1])),
            "duplicate use": lambda f: f.append(copy.deepcopy(f[0])),
        }
        for name, change in negatives.items():
            for parser in parsers:
                self.assertFalse(parser(mutated(change), marker, "claude"), name)
        # A background tool use never accepts the foreground empty placeholder.
        for parser in parsers:
            self.assertFalse(parser(text(empty(background=True) + genuine), marker, "claude"))
            self.assertFalse(parser(text(empty(background=True, detail=empty_detail(backgroundTaskId="bg_synth_01")) + genuine), marker, "claude"))
        # Contradictory genuine marker result: placeholder content with marker stdout never proves.
        contradictory = pair("toolu_synthetic_marker", "python3 synthetic_print.py", PLACEHOLDER,
                             {"stdout": marker + "\n", "stderr": "", "interrupted": False})
        for parser in parsers:
            self.assertFalse(parser(text(contradictory), marker, "claude"))
            self.assertFalse(parser(text(empty() + contradictory), marker, "claude"))

    def test_claude_pair_identity_failure_interruption_and_echo_matrix(self):
        import copy
        prompt, marker = acc.make_probe()
        self.assertTrue(prompt.startswith("Use two sequential shell tool calls. First run a separate foreground shell command"))
        self.assertIn("Immediately after the marker print finishes, run a separate foreground `sleep 60` command using the existing shell tool with a supported timeout sufficient for the 60-second hold", prompt)
        self.assertIn("Do not chain the marker print with the hold in one command, do not use a background flag, do not include the marker or nonce in the hold command, do not echo proof or emit waiting prose, and do not search for or call any monitor, task-output, or status tools or poll in a loop", prompt)
        self.assertIn("mandatory Hub first-action/completion evidence files", prompt)
        self.assertNotIn("background tool facility", prompt)
        self.assertNotIn("TaskOutput", prompt)
        self.assertNotIn("Monitor", prompt)
        self.assertLess(
            prompt.index("First run a separate foreground shell command"),
            prompt.index("Immediately after the marker print finishes, run a separate foreground `sleep 60` command"),
        )
        changes = [
            lambda f: f[1].update(session_id='99999999-2222-4333-8444-555555555555'),
            lambda f: f[0].update(session_id='not-provider-session'),
            lambda f: f[0]['message']['content'][0].update(name='Read'),
            lambda f: f[1]['message']['content'][0].update(tool_use_id='toolu_other'),
            lambda f: f[1]['message']['content'][0].update(is_error=True),
            lambda f: f[1]['message']['content'][0].pop('is_error'),
            lambda f: f[1]['tool_use_result'].update(interrupted=True),
            lambda f: f[1]['tool_use_result'].pop('interrupted'),
            lambda f: f[1]['tool_use_result'].update(stdout='different output'),
            lambda f: f[1]['tool_use_result'].update(stderr='synthetic error'),
            lambda f: f[1]['message'].update(role='assistant'),
            lambda f: f[1].pop('tool_use_result'),
            lambda f: f.append(copy.deepcopy(f[0])),
            lambda f: f.append(copy.deepcopy(f[1])),
            lambda f: f[1]['message']['content'][0].update(type='text', text=marker),
        ]
        for change in changes:
            frames = provider_frames('claude', marker); change(frames)
            self.assertFalse(acc.probe_output_ok(jsonl(frames), prompt, marker, 'claude', transport('claude')), frames)
        # The marker in tool INPUT without a real result is never output proof.
        frames = provider_frames('claude', marker)[:1]
        frames[0]['message']['content'][0]['input']['command'] = marker
        self.assertFalse(acc.probe_output_ok(jsonl(frames), prompt, marker, 'claude', transport('claude')))

        # Background-start control pairs (`run_in_background: true` + `backgroundTaskId`) are non-proof
        # and do not poison a completed foreground Bash nonce pair; 4096-byte tail at blocking monitor preserves fg pair.
        session = '11111111-2222-4333-8444-555555555555'

        def padded_frame(obj, target_len):
            raw = json.dumps(obj, separators=(',', ':'))
            self.assertLessEqual(len(raw), target_len)
            if len(raw) < target_len:
                pad_key = '_pad'
                overhead = len(json.dumps({pad_key: ''}, separators=(',', ':'))) - 1
                obj = dict(obj, **{pad_key: 'x' * max(0, target_len - len(raw) - overhead)})
                raw = json.dumps(obj, separators=(',', ':'))
            self.assertEqual(len(raw), target_len)
            return raw

        def bg_frames(task_id='bg_synth_01', cmd='sleep 60; echo done', content_override=None, stdout_val='', is_err=False, interrupted_val=False, stderr_val='', bg_flag=True):
            content_str = (
                content_override
                if content_override is not None
                else f'Command running in background with ID: {task_id}. Output is being written to: /tmp/synthetic-claude/tasks/{task_id}.output'
            )
            use_ev = {
                'type': 'assistant',
                'session_id': session,
                'message': {
                    'role': 'assistant',
                    'content': [{
                        'type': 'tool_use',
                        'id': 'toolu_synthetic_bg_hold',
                        'name': 'Bash',
                        'input': {
                            'command': cmd,
                            'description': 'Hold 60 seconds in background',
                            'run_in_background': bg_flag,
                            'timeout': 120000,
                        },
                    }],
                },
            }
            hook_1 = {'type': 'system', 'subtype': 'hook_started', 'session_id': session}
            hook_2 = {'type': 'system', 'subtype': 'hook_response', 'session_id': session, 'outcome': 'success'}
            res_ev = {
                'type': 'user',
                'session_id': session,
                'message': {
                    'role': 'user',
                    'content': [{
                        'type': 'tool_result',
                        'tool_use_id': 'toolu_synthetic_bg_hold',
                        'content': content_str,
                        'is_error': is_err,
                    }],
                },
                'tool_use_result': {
                    'stdout': stdout_val,
                    'stderr': stderr_val,
                    'interrupted': interrupted_val,
                    'isImage': False,
                    'noOutputExpected': False,
                    'backgroundTaskId': task_id,
                },
            }
            return [use_ev, hook_1, hook_2, res_ev]

        valid_fg = provider_frames('claude', marker)
        valid_bg = bg_frames()
        for combined in (valid_fg + valid_bg, valid_bg + valid_fg):
            self.assertTrue(acc.probe_output_ok(jsonl(combined), prompt, marker, 'claude', transport('claude')))

        prefix_line = padded_frame({'type': 'system', 'subtype': 'init', 'session_id': session}, 11000)
        first_action_res = padded_frame({'type': 'system', 'subtype': 'first_action', 'session_id': session}, 6259)
        bg_use_line = padded_frame(valid_bg[0], 1174)
        bg_hook1_line = padded_frame(valid_bg[1], 291)
        bg_hook2_line = padded_frame(valid_bg[2], 337)
        bg_res_line = padded_frame(valid_bg[3], 950)
        fg_use_line = padded_frame(valid_fg[0], 1106)
        fg_res_line = padded_frame(valid_fg[1], 609)
        monitor_use_line = padded_frame(
            {
                'type': 'assistant',
                'session_id': session,
                'message': {
                    'role': 'assistant',
                    'content': [{
                        'type': 'tool_use',
                        'id': 'toolu_synthetic_monitor',
                        'name': 'TaskOutput',
                        'input': {'task_id': 'bg_synth_01', 'block': True, 'timeout': 60000},
                    }],
                },
            },
            520,
        )
        mon_hook1_line = padded_frame(valid_bg[1], 291)
        mon_hook2_line = padded_frame(valid_bg[2], 337)
        stream_at_blocking_monitor = '\n'.join([
            prefix_line,
            first_action_res,
            bg_use_line,
            bg_hook1_line,
            bg_hook2_line,
            bg_res_line,
            fg_use_line,
            fg_res_line,
            monitor_use_line,
            mon_hook1_line,
            mon_hook2_line,
        ]) + '\n'
        tail_4096 = stream_at_blocking_monitor.encode('utf-8')[-4096:].decode('utf-8')
        self.assertEqual(len(tail_4096.encode('utf-8')), 4096)
        self.assertIn(fg_use_line, tail_4096)
        self.assertIn(fg_res_line, tail_4096)
        self.assertTrue(acc.probe_output_ok(tail_4096, prompt, marker, 'claude', transport('claude')))

        fg_use_1145 = padded_frame(valid_fg[0], 1145)
        fg_res_610 = padded_frame(valid_fg[1], 610)
        fg_hold_use_1102 = padded_frame(
            {
                'type': 'assistant',
                'session_id': session,
                'message': {
                    'role': 'assistant',
                    'content': [{
                        'type': 'tool_use',
                        'id': 'toolu_synthetic_fg_sleep60',
                        'name': 'Bash',
                        'input': {
                            'command': 'sleep 60',
                            'description': 'Foreground 60-second hold',
                            'timeout': 90000,
                        },
                    }],
                },
            },
            1102,
        )
        two_call_active_hold_stream = '\n'.join([
            prefix_line,
            first_action_res,
            fg_use_1145,
            fg_res_610,
            fg_hold_use_1102,
        ]) + '\n'
        two_call_tail_4096 = two_call_active_hold_stream.encode('utf-8')[-4096:].decode('utf-8')
        self.assertEqual(len(two_call_tail_4096.encode('utf-8')), 4096)
        self.assertIn(fg_use_1145, two_call_tail_4096)
        self.assertIn(fg_res_610, two_call_tail_4096)
        self.assertIn(fg_hold_use_1102, two_call_tail_4096)
        self.assertTrue(acc.probe_output_ok(two_call_tail_4096, prompt, marker, 'claude', transport('claude')))

        tok_193 = padded_frame({'type': 'system', 'subtype': 'thinking_tokens', 'session_id': session}, 193)
        tok_195 = padded_frame({'type': 'system', 'subtype': 'thinking_tokens', 'session_id': session}, 195)
        thinking_2013 = padded_frame(
            {
                'type': 'assistant',
                'session_id': session,
                'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': '', 'signature': 'sig'}]},
            },
            2013,
        )
        tool_search_use_1035 = padded_frame(
            {
                'type': 'assistant',
                'session_id': session,
                'message': {
                    'role': 'assistant',
                    'content': [{
                        'type': 'tool_use',
                        'id': 'toolu_synthetic_toolsearch',
                        'name': 'ToolSearch',
                        'input': {'query': 'select:Monitor,TaskOutput', 'max_results': 5},
                    }],
                },
            },
            1035,
        )
        tool_search_res_594 = padded_frame(
            {
                'type': 'user',
                'session_id': session,
                'message': {
                    'role': 'user',
                    'content': [{
                        'type': 'tool_result',
                        'tool_use_id': 'toolu_synthetic_toolsearch',
                        'content': "[{'type': 'tool_reference', 'tool_name': 'Monitor'}]",
                    }],
                },
            },
            594,
        )
        burst_evicted_stream = '\n'.join([
            prefix_line,
            first_action_res,
            fg_use_1145,
            fg_res_610,
            tok_193,
            tok_195,
            thinking_2013,
            tool_search_use_1035,
            tool_search_res_594,
        ]) + '\n'
        burst_tail_4096 = burst_evicted_stream.encode('utf-8')[-4096:].decode('utf-8')
        self.assertEqual(len(burst_tail_4096.encode('utf-8')), 4096)
        self.assertEqual(len(burst_tail_4096.splitlines()[0]), 60)
        self.assertFalse(acc.probe_output_ok(burst_tail_4096, prompt, marker, 'claude', transport('claude')))

        fg_with_bg_res = provider_frames('claude', marker)
        fg_with_bg_res[1]['tool_use_result']['backgroundTaskId'] = 'bg_synth_01'
        bg_with_fg_res = bg_frames()
        bg_with_fg_res[3]['tool_use_result'].pop('backgroundTaskId', None)
        bg_with_fg_res[3]['tool_use_result']['stdout'] = marker + '\n'
        bg_with_fg_res[3]['message']['content'][0]['content'] = marker + '\n'
        for neg_frames in (
            valid_bg,
            bg_frames(cmd=marker),
            bg_frames(content_override=f'Command running in background with ID: bg_synth_01.\n{marker}'),
            bg_frames(content_override=marker, stdout_val=marker),
            bg_with_fg_res,
            valid_fg + bg_with_fg_res,
            fg_with_bg_res,
            valid_fg + fg_with_bg_res,
            valid_fg + bg_frames(bg_flag=1),
            valid_fg + bg_frames(bg_flag='true'),
            valid_fg + bg_frames(task_id=''),
            valid_fg + bg_frames(task_id='bad id!'),
            valid_fg + bg_frames(content_override='Command running in background with ID: other_id. Output is being written.'),
            valid_fg + bg_frames(stdout_val='unexpected stdout'),
            valid_fg + bg_frames(stderr_val='unexpected stderr'),
            valid_fg + bg_frames(is_err=True),
            valid_fg + bg_frames(interrupted_val=True),
        ):
            self.assertFalse(acc.probe_output_ok(jsonl(neg_frames), prompt, marker, 'claude', transport('claude')), neg_frames)

    def test_argon_read_file_is_validated_nonproof_around_genuine_run_command(self):
        import copy
        SESSION = '11111111-2222-4333-8444-555555555555'
        NONCE = 'SYNTHETIC-NONCE:0123456789abcdef'
        WRAP = ('Created At: 2026-01-01T00:00:00+00:00\nCompleted At: 2026-01-01T00:00:01+00:00\n'
                'Command exited with code 0.\n\nCommand output:\n')

        def pair(step, tool, params, output):
            def frame(state, info):
                return {'event': 'step_update', 'step_update': {
                    'conversation_id': SESSION, 'step_index': step, 'state': state,
                    'step_type': 'tool', 'tool_name': tool, 'tool_info': info}}
            return [frame('ACTIVE', {'name': tool, 'parameters': dict(params)}),
                    frame('DONE', {'name': tool, 'parameters': dict(params), 'output': output})]

        def read_file(step, params=None, output='synthetic file text'):
            return pair(step, 'read_file', {'Path': 'synthetic/readme.txt'} if params is None else params, output)

        command = pair(7, 'run_command', {'CommandLine': 'synthetic nonce command'}, WRAP + NONCE + '\n')

        def text(frames):
            return '\n'.join(json.dumps(f, separators=(',', ':')) for f in frames) + '\n'

        for verdict in [lambda t, m: acc.paired_stream_output_ok(t, m, 'antigravity'),
                    lambda t, m: acc.probe_output_ok(t, 'synthetic prompt', m, 'antigravity', transport('antigravity'))]:
            # Valid read_file before/after/around the genuine command pair is accepted.
            for frames in (read_file(4) + command, command + read_file(9), read_file(4) + command + read_file(9),
                           read_file(4, {'Path': 'a', 'StartLine': 700}) + command,
                           read_file(4, {'Path': 'a', 'StartLine': 600, 'EndLine': 1400}) + command,
                           read_file(4, {'Path': 'a', 'StartLine': 5, 'EndLine': 5}) + command,
                           read_file(4)[:1] + command):
                self.assertTrue(verdict(text(frames), NONCE), frames)
            # read_file alone never proves, whatever its output or parameters embed.
            fake_command = json.dumps(command[1])
            for output in (NONCE, WRAP + NONCE + '\n', fake_command, NONCE + '\n' + fake_command):
                self.assertFalse(verdict(text(read_file(4, output=output)), NONCE), output)
            self.assertFalse(verdict(text(read_file(4, {'Path': NONCE})), NONCE))
            # A read_file frame never substitutes for or alters the command proof.
            self.assertFalse(verdict(text(read_file(4) + command[:1]), NONCE))
            self.assertFalse(verdict(text(read_file(4, output=WRAP + NONCE + '\n') + command[:1]), NONCE))
            self.assertFalse(verdict(text(read_file(4) + pair(7, 'run_command', {'CommandLine': 'x'}, WRAP + 'other\n')), NONCE))
            # Malformed read_file parameters stay rejected even beside a valid proof.
            bad_params = [
                {}, {'StartLine': 1}, {'Path': ''}, {'Path': 5}, {'Path': None}, {'Path': ['a']}, {'Path': 'a\0b'},
                {'Path': 'a', 'Extra': 1}, {'Path': 'a', 'path': 'b'}, {'Path': 'a', 'CommandLine': 'x'},
                {'Path': 'a', 'StartLine': '1'}, {'Path': 'a', 'StartLine': 1.0}, {'Path': 'a', 'StartLine': True},
                {'Path': 'a', 'StartLine': None}, {'Path': 'a', 'StartLine': -1}, {'Path': 'a', 'StartLine': 2 ** 31},
                {'Path': 'a', 'EndLine': '9'}, {'Path': 'a', 'EndLine': False}, {'Path': 'a', 'EndLine': -3},
                {'Path': 'a', 'StartLine': 10, 'EndLine': 9},
            ]
            for params in bad_params:
                self.assertFalse(verdict(text(read_file(4, params) + command), NONCE), params)
            for bad in ({'Path': 'a'}, {'Path': 'a', 'StartLine': 1}):
                for name in ('other_file_tool', 'Read_File', 'read_files'):
                    frames = pair(4, name, bad, 'x') + command
                    self.assertFalse(verdict(text(frames), NONCE), name)
            # Envelope guards still apply to read_file steps.
            def mutated(change):
                frames = read_file(4) + command
                change(frames)
                return text(frames)
            changes = [
                lambda f: f[1]['step_update']['tool_info'].update(output=None),
                lambda f: f[1]['step_update']['tool_info'].update(output=5),
                lambda f: f[1]['step_update']['tool_info'].pop('output'),
                lambda f: f[0]['step_update']['tool_info'].update(output='early'),
                lambda f: f[1]['step_update']['tool_info']['parameters'].update(Path='changed'),
                lambda f: f[1]['step_update']['tool_info'].update(name='run_command'),
                lambda f: f[1]['step_update']['tool_info'].update(parameters='not-an-object'),
                lambda f: f[1]['step_update'].update(state='ERROR'),
                lambda f: f[1]['step_update'].update(state='FAILED'),
                lambda f: f[1]['step_update'].update(error='synthetic error'),
                lambda f: f[1]['step_update']['tool_info'].update(is_error=True),
                lambda f: f[1].update(interrupted=True),
                lambda f: f[0]['step_update'].update(conversation_id='not-a-uuid'),
                lambda f: f[0]['step_update'].update(step_index=True),
                lambda f: f[0]['step_update'].update(step_index=-1),
                lambda f: f.append(copy.deepcopy(f[0])),
                lambda f: f.append(copy.deepcopy(f[1])),
                lambda f: f.insert(2, copy.deepcopy(f[1])),
            ]
            for change in changes:
                self.assertFalse(verdict(mutated(change), NONCE), mutated(change))
            # Reused step identity across read_file and the command is rejected.
            self.assertFalse(verdict(text(read_file(7) + command), NONCE))
            # Malformed JSON and the capture byte bound are unchanged.
            self.assertFalse(verdict(text(read_file(4) + command) + '{"event":"step_update"\n', NONCE))
            self.assertTrue(verdict('{"clipped partial frame\n' + text(read_file(4) + command), NONCE))
            self.assertFalse(verdict(text(read_file(4, output='x' * 70000) + command), NONCE))

    def test_argon_pair_identity_params_and_native_exit_wrapper_matrix(self):
        import copy
        prompt, marker = acc.make_probe()
        changes = [
            lambda f: f[1]['step_update'].update(conversation_id='99999999-2222-4333-8444-555555555555'),
            lambda f: f[1]['step_update'].update(step_index=8),
            lambda f: f[1]['step_update'].update(step_index=True),
            lambda f: f[1]['step_update'].update(tool_name='other_provider_tool'),
            lambda f: f[1]['step_update']['tool_info'].update(name='other_tool'),
            lambda f: f[1]['step_update']['tool_info']['parameters'].update(CommandLine='different command'),
            lambda f: f[1]['step_update'].update(state='ERROR'),
            lambda f: f[1]['step_update'].update(error='synthetic error'),
            lambda f: f[1]['step_update']['tool_info'].update(interrupted=True),
            lambda f: f[0]['step_update']['tool_info'].update(output=marker),
            lambda f: f.append(copy.deepcopy(f[0])),
            lambda f: f.append(copy.deepcopy(f[1])),
        ]
        for change in changes:
            frames = provider_frames('antigravity', marker); change(frames)
            self.assertFalse(acc.probe_output_ok(jsonl(frames), prompt, marker, 'antigravity', transport('antigravity')), frames)
        valid = provider_frames('antigravity', marker)[1]['step_update']['tool_info']['output']
        outputs = [marker, 'Command exited with code 0.\n\nCommand output:\n' + marker,
                   valid.replace('code 0.', 'code 1.') + '\nCommand exited with code 0.\n' + marker,
                   valid.replace('Command output:', 'Assistant output:'),
                   valid.replace('Created At: 2026-01-01T00:00:00+00:00', 'Created At: invalid'),
                   valid.replace('Completed At: 2026-01-01T00:00:01+00:00', 'Completed At: 2025-01-01T00:00:01+00:00')]
        for output in outputs:
            frames = provider_frames('antigravity', marker)
            frames[1]['step_update']['tool_info']['output'] = output
            self.assertFalse(acc.probe_output_ok(jsonl(frames), prompt, marker, 'antigravity', transport('antigravity')), output)
        frames = provider_frames('antigravity', marker)[:1]
        frames[0]['step_update']['tool_info']['parameters']['CommandLine'] = marker
        self.assertFalse(acc.probe_output_ok(jsonl(frames), prompt, marker, 'antigravity', transport('antigravity')))

        session = '11111111-2222-4333-8444-555555555555'

        def padded_frame(obj, target_len):
            raw = json.dumps(obj, separators=(',', ':'))
            self.assertLessEqual(len(raw), target_len)
            if len(raw) < target_len:
                pad_key = '_pad'
                overhead = len(json.dumps({pad_key: ''}, separators=(',', ':'))) - 1
                obj = dict(obj, **{pad_key: 'x' * max(0, target_len - len(raw) - overhead)})
                raw = json.dumps(obj, separators=(',', ':'))
            self.assertEqual(len(raw), target_len)
            return raw

        def argon_tool_pair(step_idx, tool_name, params, output_str, *, active_name=None, done_name=None, done_params=None, done_state='DONE'):
            act_info = {'name': tool_name if active_name is None else active_name, 'parameters': dict(params)}
            done_info = {
                'name': tool_name if done_name is None else done_name,
                'parameters': dict(params if done_params is None else done_params),
                'output': output_str,
            }
            return [
                {'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': step_idx, 'state': 'ACTIVE',
                 'step_type': 'tool', 'tool_name': tool_name, 'tool_info': act_info}},
                {'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': step_idx, 'state': done_state,
                 'step_type': 'tool', 'tool_name': tool_name, 'tool_info': done_info}},
            ]

        argon_fg = argon_tool_pair(
            6, 'run_command', {'CommandLine': "printf '%s%s\\n' OK: synth"},
            f'Created At: 2026-01-01T00:00:00+00:00\nCompleted At: 2026-01-01T00:00:01+00:00\nCommand exited with code 0.\n\nCommand output:\n{marker}\r\n',
        )
        argon_bg_hold = argon_tool_pair(4, 'run_command', {'CommandLine': 'sleep 60'}, 'Command exited with code 0.\n\nNo output from the command.')
        argon_manage = argon_tool_pair(
            8, 'manage_task', {'Action': 'status', 'TaskId': f'{session}/task-4'},
            f'Created At: 2026-01-01T00:00:02+00:00\nCompleted At: 2026-01-01T00:00:02+00:00\nTask: {session}/task-4\nStatus: RUNNING\nLog: file:///tmp/synthetic-argon/tasks/task-4.log\n',
        )
        argon_schedule = argon_tool_pair(
            10, 'schedule', {'DurationSeconds': 60, 'Prompt': 'Background hold task finished.'},
            f'Timer cancelled early: its early-termination condition ({session}/task-4) was met by a message from {session}/task-4.',
        )
        argon_cmd_status = argon_tool_pair(12, 'command_status', {'CommandId': 'cmd-synth-4', 'WaitDurationSeconds': 5}, 'Status: RUNNING')
        argon_wait = argon_tool_pair(14, 'wait', {'seconds': '5'}, 'Waited.')
        argon_wait5 = argon_tool_pair(16, 'wait_5_seconds', {}, 'Waited 5 seconds.')

        for combo in (
            argon_bg_hold + argon_fg + argon_manage + argon_schedule,
            argon_manage + argon_schedule + argon_fg,
            argon_fg + argon_manage + argon_schedule[:1],
            argon_fg + argon_cmd_status + argon_wait + argon_wait5,
        ):
            self.assertTrue(acc.probe_output_ok(jsonl(combo), prompt, marker, 'antigravity', transport('antigravity')), combo)

        ar_init = padded_frame({'event': 'init', 'conversation_id': session}, 1340)
        ar_s0 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 0, 'state': 'DONE', 'step_type': 'user_input'}}, 151)
        ar_s1 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 1, 'state': 'DONE', 'step_type': 'agent_response'}}, 300)
        ar_s2_act = padded_frame(argon_tool_pair(2, 'run_command', {'CommandLine': 'aplexer whoami --json'}, '')[0], 422)
        ar_s2_done = padded_frame(argon_tool_pair(2, 'run_command', {'CommandLine': 'aplexer whoami --json'}, 'Command exited with code 0.\n\nNo output from the command.')[1], 601)
        ar_s3 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 3, 'state': 'DONE', 'step_type': 'agent_response'}}, 301)
        ar_s4_act = padded_frame(argon_bg_hold[0], 248)
        ar_s4_done = padded_frame(argon_bg_hold[1], 348)
        ar_s5 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 5, 'state': 'DONE', 'step_type': 'agent_response'}}, 303)
        ar_s6_act = padded_frame(argon_fg[0], 289)
        ar_s6_done = padded_frame(argon_fg[1], 484)
        ar_s7 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 7, 'state': 'DONE', 'step_type': 'agent_response'}}, 303)
        ar_s8_act = padded_frame(argon_manage[0], 296)
        ar_s8_done = padded_frame(argon_manage[1], 705)
        ar_s9 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 9, 'state': 'DONE', 'step_type': 'agent_response'}}, 304)
        ar_s10_act = padded_frame(argon_schedule[0], 281)
        ar_s10_done = padded_frame(argon_schedule[1], 494)
        ar_s11 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 11, 'state': 'DONE', 'step_type': 'agent_response'}}, 327)
        ar_s12 = padded_frame({'event': 'step_update', 'step_update': {'conversation_id': session, 'step_index': 12, 'state': 'DONE', 'step_type': 'system_message'}}, 187)
        argon_full_stream = '\n'.join([
            ar_init, ar_s0, ar_s1, ar_s2_act, ar_s2_done, ar_s3, ar_s4_act, ar_s4_done,
            ar_s5, ar_s6_act, ar_s6_done, ar_s7, ar_s8_act, ar_s8_done, ar_s9,
            ar_s10_act, ar_s10_done, ar_s11, ar_s12,
        ]) + '\n'
        argon_tail_4096 = argon_full_stream.encode('utf-8')[-4096:].decode('utf-8')
        self.assertEqual(len(argon_tail_4096.encode('utf-8')), 4096)
        self.assertEqual(len(argon_tail_4096.splitlines()), 12)
        self.assertEqual(len(argon_tail_4096.splitlines()[0]), 111)
        self.assertTrue(acc.probe_output_ok(argon_tail_4096, prompt, marker, 'antigravity', transport('antigravity')))

        forged_wrapped_marker = argon_fg[1]['step_update']['tool_info']['output']
        collision_cmd_to_manage = [
            argon_fg[0],
            argon_tool_pair(6, 'manage_task', {'Action': 'status', 'TaskId': f'{session}/task-4'}, 'ok')[1],
        ]
        collision_manage_to_cmd = [
            argon_tool_pair(6, 'manage_task', {'Action': 'status', 'TaskId': f'{session}/task-4'}, 'ok')[0],
            argon_fg[1],
        ]
        control_error = argon_tool_pair(14, 'manage_task', {'Action': 'kill', 'TaskId': f'{session}/task-10'}, '', done_state='ERROR')
        control_error[1]['step_update']['tool_info'].pop('output', None)
        control_error[1]['step_update']['tool_info']['error'] = {'type': 'TOOL_ERROR', 'message': 'not running'}
        control_active_with_output = argon_tool_pair(8, 'manage_task', {'Action': 'status', 'TaskId': f'{session}/task-4'}, 'ok')
        control_active_with_output[0]['step_update']['tool_info']['output'] = 'unexpected'
        control_done_nonstring_out = argon_tool_pair(8, 'manage_task', {'Action': 'status', 'TaskId': f'{session}/task-4'}, 'ok')
        control_done_nonstring_out[1]['step_update']['tool_info']['output'] = 123

        for neg_frames in (
            argon_manage + argon_schedule,
            argon_bg_hold + argon_manage + argon_schedule,
            argon_tool_pair(8, 'manage_task', {'Action': 'status', 'TaskId': marker}, forged_wrapped_marker),
            argon_tool_pair(10, 'schedule', {'DurationSeconds': 60, 'Prompt': marker}, forged_wrapped_marker),
            argon_fg[1:] + argon_manage + argon_schedule,
            argon_fg + argon_tool_pair(8, 'manage_task', {'Action': 'status', 'TaskId': 't1'}, 'ok', done_name='schedule'),
            argon_fg + argon_tool_pair(8, 'manage_task', {'Action': 'status', 'TaskId': 't1'}, 'ok', active_name='run_command'),
            argon_fg + argon_tool_pair(8, 'manage_task', {'Action': 'status', 'TaskId': 't1'}, 'ok', done_params={'Action': 'status', 'TaskId': 't2'}),
            argon_fg + argon_tool_pair(10, 'schedule', {'DurationSeconds': 60, 'Prompt': 'p1'}, 'ok', done_params={'DurationSeconds': 30, 'Prompt': 'p1'}),
            collision_cmd_to_manage,
            argon_fg + collision_cmd_to_manage,
            collision_manage_to_cmd,
            argon_fg + [argon_manage[0], argon_manage[0], argon_manage[1]],
            argon_fg + [argon_manage[0], argon_manage[1], argon_manage[1]],
            argon_fg + [argon_manage[1], argon_manage[0]],
            argon_fg + control_error,
            argon_fg + control_active_with_output,
            argon_fg + control_done_nonstring_out,
            argon_fg + argon_tool_pair(8, 'unknown_control_tool', {'Action': 'status'}, 'ok'),
            argon_fg + argon_tool_pair(8, 'run_command', {'CommandLine': 'true', 'Extra': 'bad'}, forged_wrapped_marker),
            argon_fg + argon_tool_pair(8, 'run_command', {'CommandLine': ''}, forged_wrapped_marker),
            argon_fg + argon_tool_pair(8, 'manage_task', {'Action': 'unknown_action'}, 'ok'),
            argon_fg + argon_tool_pair(8, 'manage_task', {'Action': 'status', 'Extra': 'bad'}, 'ok'),
            argon_fg + argon_tool_pair(8, 'manage_task', {'Action': 'status', 'TaskId': ''}, 'ok'),
            argon_fg + argon_tool_pair(10, 'schedule', {'DurationSeconds': 0, 'Prompt': 'wait'}, 'ok'),
            argon_fg + argon_tool_pair(10, 'schedule', {'DurationSeconds': True, 'Prompt': 'wait'}, 'ok'),
            argon_fg + argon_tool_pair(10, 'schedule', {'DurationSeconds': 60, 'Prompt': ''}, 'ok'),
            argon_fg + argon_tool_pair(10, 'schedule', {'DurationSeconds': 60, 'Prompt': 'wait', 'Unknown': 'x'}, 'ok'),
            argon_fg + argon_tool_pair(16, 'wait_5_seconds', {'unexpected': 'arg'}, 'ok'),
            argon_fg + argon_tool_pair(14, 'wait', {}, 'ok'),
        ):
            self.assertFalse(acc.probe_output_ok(jsonl(neg_frames), prompt, marker, 'antigravity', transport('antigravity')), neg_frames)

    def test_transport_attestation_bounds_and_same_release_are_required(self):
        import copy
        path = Path(evidence()); original = json.loads(path.read_text())
        mutations = [lambda x: x.pop('capture_transports'), lambda x: x.update(capture_transports={}),
                     lambda x: x['capture_transports'].update(unknown=transport('unknown')),
                     lambda x: x['capture_transports']['go'].update(host_release_sha='b' * 40),
                     lambda x: x['capture_transports']['codex'].update(format='opencode_json_v1'),
                     lambda x: x['capture_transports']['zai'].update(provider_version=''),
                     lambda x: x['capture_transports']['zai'].update(format='zcode_jsonl_v1'),
                     lambda x: x['capture_transports']['go'].update(profile_set_sha256='bad')]
        for mutate in mutations:
            ev = copy.deepcopy(original); mutate(ev['operator_validation']); path.write_text(json.dumps(ev))
            self.assertTrue(acc.check_evidence(path, COMMIT), ev)
        ev = copy.deepcopy(original)
        receipt = path.with_suffix('.capture-receipt'); receipt.write_bytes(b'offline capture attestation')
        self.addCleanup(receipt.unlink)
        ev['operator_validation']['capture_transports']['go'].update(path=receipt.name, sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
        path.write_text(json.dumps(ev)); self.assertEqual(acc.check_evidence(path, COMMIT), [])
        receipt.write_bytes(b'changed')
        self.assertTrue(any('hash mismatch' in e for e in acc.check_evidence(path, COMMIT)))

    def test_checked_evidence_hashes_the_single_validated_buffer(self):
        path = evidence()
        raw = Path(path).read_bytes()
        with mock.patch.object(acc, 'bounded_file', return_value=raw) as read:
            problems, digest, transports = acc.checked_evidence(path, COMMIT)
        self.assertEqual(problems, [])
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(transports, json.loads(raw)['operator_validation']['capture_transports'])
        read.assert_called_once_with(path, acc.EVIDENCE_LIMIT)

    def test_capture_attestation_and_hash_use_same_validated_buffer(self):
        path = Path(evidence()); raw = path.read_bytes()
        expected = json.loads(raw)['operator_validation']['capture_transports']
        original = acc.validate_evidence
        def replace_after_validate(data, filename, commit):
            problems = original(data, filename, commit)
            changed = json.loads(raw)
            changed['operator_validation']['capture_transports'].pop('codex')
            path.write_text(json.dumps(changed))
            return problems
        with mock.patch.object(acc, 'validate_evidence', side_effect=replace_after_validate):
            problems, digest, actual = acc.checked_evidence(path, COMMIT)
        self.assertEqual(problems, [])
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(actual, expected)
        self.assertIn('codex', actual)
        self.assertNotIn('codex', json.loads(path.read_text())['operator_validation']['capture_transports'])

    def test_operator_validation_cannot_be_replaced_by_four_green_strings(self):
        path = Path(evidence())
        original = json.loads(path.read_text())
        for mutation in [lambda ev: ev.pop('kind'), lambda ev: ev.pop('operator_validation'),
                         lambda ev: ev.update(operator_validation=True),
                         lambda ev: ev['operator_validation'].update(actor=''),
                         lambda ev: ev['operator_validation'].update(target_commit='b' * 40),
                         lambda ev: ev['operator_validation']['receipts'].pop('deployment'),
                         lambda ev: ev['operator_validation']['receipts']['ci_e2e'].update(commit='b' * 40),
                         lambda ev: ev['operator_validation']['receipts']['canary'].update(sha256='forged'),
                         lambda ev: ev['operator_validation']['receipts']['deployment'].update(reference='unsafe\nreference')]:
            import copy
            ev = copy.deepcopy(original); mutation(ev); path.write_text(json.dumps(ev))
            self.assertTrue(acc.check_evidence(path, COMMIT), ev)

    def test_local_receipt_bytes_must_match_operator_sha_pin(self):
        path = Path(evidence());ev = json.loads(path.read_text())
        receipt = path.with_suffix('.receipt');receipt.write_bytes(b'nonsecret offline deployment receipt')
        self.addCleanup(receipt.unlink)
        pin = ev['operator_validation']['receipts']['deployment']
        pin.update(path=receipt.name, sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
        path.write_text(json.dumps(ev));self.assertEqual(acc.check_evidence(path, COMMIT), [])
        receipt.write_bytes(b'changed receipt')
        self.assertTrue(any('hash mismatch' in error for error in acc.check_evidence(path, COMMIT)))
        receipt.unlink()
        self.assertTrue(any('unreadable' in error for error in acc.check_evidence(path, COMMIT)))
        receipt.write_bytes(b'restored')

    def test_evidence_and_local_receipts_are_bounded_regular_files(self):
        path = Path(evidence());original = path.read_bytes()
        path.write_bytes(b' ' * (acc.EVIDENCE_LIMIT + 1))
        self.assertTrue(acc.check_evidence(path, COMMIT))
        path.write_bytes(original)
        ev = json.loads(original)
        receipt = path.with_suffix('.receipt');receipt.write_bytes(b'x' * (acc.RECEIPT_LIMIT + 1))
        self.addCleanup(receipt.unlink)
        ev['operator_validation']['receipts']['deployment'].update(path=receipt.name)
        path.write_text(json.dumps(ev));self.assertTrue(acc.check_evidence(path, COMMIT))
        receipt.unlink();receipt.symlink_to(path)
        self.assertTrue(acc.check_evidence(path, COMMIT))

    def test_current_picker_strict_contract(self):
        import copy
        row = fixture('hub_sessions_show.json')['session']
        self.assertEqual(acc.picker_problems(row['picker'], row), [])
        for mutate in [lambda p: p.update(attempt=1.0), lambda p: p.update(policy_version=None),
                       lambda p: p['history'][0].update(machine_id=''),
                       lambda p: p['pool'][0].update(share=0.8),
                       lambda p: p['pool'][0].update(weight=float('inf'))]:
            picker = copy.deepcopy(row['picker']); mutate(picker)
            self.assertTrue(acc.picker_problems(picker, row))



if __name__ == '__main__':
    unittest.main()
