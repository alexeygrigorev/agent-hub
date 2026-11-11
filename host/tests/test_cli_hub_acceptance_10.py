"""Offline tests for scripts/hub_acceptance_10.py.

Fake `hub`, `a` and alert-sample binaries are put on PATH; their responses model the maintained CLI contract with synthetic fixtures in
host/tests/fixtures/acceptance/. No Hub access, no real session is started or stopped."""
import importlib.util
import io
import json
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

SOL, GLM, MUSE, LUNA = 'gpt-6.1-sol', 'glm-5.3-flash', 'opencode-go/muse-spark-1.3-contributor', 'gpt-6-luna'  # fixture pool
OK_MODELS = [SOL, GLM, MUSE, SOL, GLM, MUSE, SOL, GLM, LUNA, SOL]


def fixture(name):
    return json.loads((FIX / name).read_text())


COMMIT = 'a' * 40
WORKSPACE = '/home/user/work/acceptance'
TMP = HERE.parents[1] / '.local/tmp'
TMP.mkdir(parents=True, exist_ok=True)


def transport(engine='codex', commit=COMMIT):
    # Synthetic attestations only: no claim about an installed provider/host.
    return {'engine': engine, 'machine_id': acc.HOST,
            'format': acc.CAPTURE_FORMATS.get(engine, 'plaintext'), 'host_release_sha': commit,
            'provider_version': 'offline-fixture-v1',
            'profile_set_sha256': hashlib.sha256(('nonsecret-offline-argv-' + engine).encode()).hexdigest(),
            'reference': 'synthetic://capture/' + engine,
            'sha256': hashlib.sha256(('offline-capture-receipt-' + engine).encode()).hexdigest()}


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


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='acc10-', dir=TMP))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for name in ('hub', 'a', 'alerts'):
            (self.bin / name).symlink_to(FIX / 'fake_hub.py')
        self.cfg_path = self.tmp / 'cfg.json'
        env = {'PATH': '%s:%s' % (self.bin, os.environ['PATH']), 'FAKE_CFG': str(self.cfg_path)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Fake CLI processes import the real command parser; their cold startup
        # can exceed0.5s under the shared lane. Keep proof assertions unchanged
        # with an offline budget well below the production90s capture budget.
        for name, val in (('CAPTURE_BUDGET_S', 2),):
            p = mock.patch.object(acc, name, val)
            p.start()
            self.addCleanup(p.stop)

    def state(self):
        path = Path(str(self.cfg_path) + '.state')
        return json.loads(path.read_text()) if path.exists() else {'sessions': {}}

    def go(self, **cfg):
        bundle = cfg.pop('evidence_bundle', None)
        cfg.setdefault('models', OK_MODELS)
        Path(str(self.cfg_path) + '.state').unlink(missing_ok=True)
        self.cfg_path.write_text(json.dumps(cfg))
        out = io.StringIO()
        r = acc.Runner(alerts_cmd=str(self.bin / 'alerts'))
        rc = acc.run(r, bundle or evidence(), COMMIT, workspace=WORKSPACE, sleep=lambda s: time.sleep(0.01), out=out, intake='intake-1')
        return rc, out.getvalue()

    def assertAllStopped(self):
        st = self.state()
        self.assertTrue(st['sessions'])
        for s in st['sessions'].values():
            self.assertTrue(s['stopped'], 'leaked session %s' % s['id'])


class FixtureStateTests(Harness):
    def seed_state(self):
        self.cfg_path.write_text('{}')
        state = {'n': 0, 'sessions': {}, 'keys': {}, 'log': []}
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
        # Block exactly after the complete temporary is closed, before replace.
        # The pipe handshake removes timing guesses and needs no slow/hung CLI.
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
        self.assertNotIn('pick', self.state()['log'])
        summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
        self.assertEqual(len(summary['starts'][0]['picker']['pool']), 4)
        self.assertEqual(summary['starts'][0]['picker']['policy_version'], 7)
        # Each capture/list proof is followed by stop before the next start.
        sequence = [x for x in self.state()['log'] if x == 'start' or x.startswith('stop:')]
        self.assertEqual(sequence[::2], ['start'] * 10)
        self.assertEqual(self.state()['log'].count('start:--intake-task-id'), 10)

    def test_wrong_show_identity_is_refused_and_known_start_is_stopped(self):
        rc, out = self.go(wrong_show_id=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('show session id mismatch', out)
        self.assertAllStopped()
        self.assertEqual(self.state()['n'], 1)

    def test_malformed_native_uuid_never_proves_presence_or_cleanup_absence(self):
        for value in ('native-placeholder', '1234', 'AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE', None, [], {'id': 'untrusted'}):
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
                self.assertIn('no completed tool output', out)
                self.assertAllStopped()

    def test_unsupported_plaintext_json_and_missing_attestation_fail_with_cleanup(self):
        for engine in ('claude', 'antigravity'):
            pool = [{'policy_id': 'synthetic-' + engine, 'model': 'synthetic-model', 'engine': engine,
                     'effort': None, 'weight': 1, 'share': 1}]
            with self.subTest(engine=engine):
                rc, out = self.go(pool=pool, models=['synthetic-model'] * 10, capture_format_override='go')
                self.assertEqual(rc, 1, out)
                self.assertIn('no verified stream transport for engine ' + engine, out)
                self.assertAllStopped()
        path = Path(evidence()); ev = json.loads(path.read_text())
        ev['operator_validation']['capture_transports'].pop('codex'); path.write_text(json.dumps(ev))
        rc, out = self.go(evidence_bundle=str(path))
        self.assertEqual(rc, 1, out); self.assertIn('no verified stream transport for engine codex', out)
        self.assertAllStopped()

    def test_wrong_stream_for_current_engine_is_not_proof(self):
        rc, out = self.go(capture_format_override='go')
        self.assertEqual(rc, 1, out); self.assertIn('no completed tool output', out)
        self.assertAllStopped()

    def test_operator_bundle_must_not_change_during_acceptance(self):
        original = acc.checked_evidence
        calls = []
        def check(path, commit):
            calls.append(path)
            if len(calls) == 2:
                data = json.loads(Path(path).read_text())
                data['operator_validation']['actor'] = 'different-offline-validator'
                Path(path).write_text(json.dumps(data))
            return original(path, commit)
        with mock.patch.object(acc, 'checked_evidence', side_effect=check):
            rc, out = self.go()
        self.assertEqual(rc, 1, out)
        self.assertIn('evidence bundle changed during run', out)
        self.assertAllStopped()

    def test_replacement_after_validation_cannot_become_the_baseline_hash(self):
        original = acc.validate_evidence
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
        with mock.patch.object(acc, 'validate_evidence', side_effect=validate_then_replace):
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
        rc, out = self.go(fallback=True)
        self.assertEqual(rc, 0, out)
        self.assertAllStopped()
        rc, out = self.go(fallback=True, cross_attempt_pool=True)
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

    def test_unacknowledged_capture_cannot_prove_output(self):
        rc, out = self.go(capture_unacked=True)
        self.assertEqual(rc, 1)
        self.assertIn('no completed tool output', out)
        self.assertAllStopped()

    def test_refuses_without_ready_env(self):
        os.environ.pop('HUB_ACCEPTANCE_READY', None)
        self.assertEqual(acc.main(['--live']), 2)
        self.assertEqual(acc.main([]), 2)

    def test_acceptance_criteria_source_required(self):
        self.cfg_path.write_text(json.dumps({'models': OK_MODELS}))
        out = io.StringIO()
        rc = acc.run(acc.Runner(alerts_cmd=str(self.bin / 'alerts')), evidence(), COMMIT, workspace=WORKSPACE, out=out)
        self.assertEqual(rc, 1)
        self.assertIn('acceptance criteria source missing', out.getvalue())
        self.assertEqual(self.state()['sessions'], {}, 'must not start anything')

    def test_start_failure_ends_run(self):
        rc, out = self.go(fail_start_at=3)
        self.assertEqual(rc, 1)
        self.assertIn('start failed', out)
        # A definite refusal ends the run and must not trigger a new launch via replay.
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
                self.assertIn('original key ' + record['key'], out)
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
        self.assertFalse(sessions[2]['stopped'])  # explicitly unknown to the client, never falsely reported clean
        summary = json.loads(out[out.index('{\n'):out.rindex('\nACCEPTANCE ')])
        self.assertTrue(summary['starts'][2]['cleanup_uncertain'])
        self.assertEqual(len(self.state()['start_requests']), 5)  # two good, one lost, two same-key receipts

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
        rc, out = self.go(models=[LUNA] * 4 + [SOL, GLM, MUSE, SOL, GLM, MUSE])
        self.assertEqual(rc, 1)
        self.assertIn('luna 4 > 3', out)

    def test_luna_on_degraded_pool(self):
        one = [fixture('hub_sessions_pick.json')['pool'][0]]
        one[0]['share'] = 1.0
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
        self.assertIn("host None != hetzner-aplexer", out)
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
        self.assertIn('no completed tool output beyond the prompt', out)

    def test_stop_not_confirmed(self):
        rc, out = self.go(stop_not_confirmed=True)
        self.assertEqual(rc, 1)
        self.assertIn('!= stopped', out)

    def test_native_lingers_after_stop_but_exited_rows_do_not_count(self):
        with mock.patch.object(acc, 'STOP_BUDGET_S', 6):
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
        evidence = summary['alert_watermarks']
        before, after = evidence['before'], evidence['after']
        self.assertEqual(after['generation'], before['generation'])
        self.assertEqual(after['count'], before['count'])
        self.assertGreater(after['observed_at_ms'], before['observed_at_ms'])
        self.assertLess(after['observed_at_ms'], evidence['required_post_observed_at_ms'])
        self.assertLess(evidence['required_post_observed_at_ms'] - after['observed_at_ms'], acc.ALERT_SAMPLE_MAX_AGE_MS)
        self.assertNotIn('stale or from the future', out)
        self.assertNotIn('reset or observation time regressed', out)

    def test_historical_occurrences_do_not_count_but_broken_sampler_fails_closed(self):
        rc, out = self.go(alert_count_before=40)
        self.assertEqual(rc, 0, out)
        rc, out = self.go(alerts_broken=True)
        self.assertEqual(rc, 1)
        self.assertIn('alert baseline unavailable', out)
        self.assertEqual(self.state()['sessions'], {})


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
        for engine in acc.CAPTURE_FORMATS:
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

    def test_transport_attestation_bounds_and_same_release_are_required(self):
        import copy
        path = Path(evidence()); original = json.loads(path.read_text())
        mutations = [lambda x: x.pop('capture_transports'), lambda x: x.update(capture_transports={}),
                     lambda x: x['capture_transports'].update(claude=transport('claude')),
                     lambda x: x['capture_transports']['go'].update(host_release_sha='b' * 40),
                     lambda x: x['capture_transports']['codex'].update(format='opencode_json_v1'),
                     lambda x: x['capture_transports']['zai'].update(provider_version=''),
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
