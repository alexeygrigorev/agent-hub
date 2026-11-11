"""Owned-temp offline evidence contracts. No producer, service or mail is invoked."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('hub_alert_evidence', ROOT / 'scripts/hub_alert_evidence.py')
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)
NOW = 1700000000000


@unittest.skipUnless(sys.platform == 'linux', 'private evidence backend requires Linux fd/flock semantics')
class AlertEvidenceTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(prefix='alert-evidence-', dir=parent))
        self.addCleanup(shutil.rmtree, self.temp)
        self.state = self.temp / 'ledger'
        self.controller = self.temp / 'controller'
        self.controller.mkdir(mode=0o700)
        self.artifact = self.temp / 'synthetic-instrumented-runtime.py'
        self.artifact.write_text('# synthetic producer and independent controller source\n')
        self.artifact.chmod(0o600)
        artifacts = [{'path': str(self.artifact), 'sha256': hashlib.sha256(self.artifact.read_bytes()).hexdigest()}]
        self.source = evidence.digest(artifacts)
        self.proofs = {}
        self.config = {'kind': 'hub_alert_enrollment_v1', 'recipient': evidence.RECIPIENT, 'producers': {}}
        for producer in sorted(evidence.REQUIRED_PRODUCERS):
            mode = 'external' if producer == 'picker_control' else 'ledger'
            self.config['producers'][producer] = {'mode': mode, 'artifacts': copy.deepcopy(artifacts),
                'source_id': self.source, 'witness_path': str(self.controller / (producer + '.json'))}
            self.proofs[producer] = {'kind': 'hub_controller_coverage_v1', 'producer': producer,
                'source_id': self.source, 'epoch': 'epoch-1', 'controller_id': 'controller-boot-1',
                'limitations': [], 'complete': True, 'started_total': 0, 'finished_total': 0,
                'baseline_count': 0, 'baseline_invocations': 0, 'observed_at_ms': NOW, 'occurrence_count': 0}
            self.write_proof(producer)

    def write_proof(self, producer, **changes):
        self.proofs[producer].update(changes)
        path = self.controller / (producer + '.json')
        with evidence.PrivateDirectory(self.controller) as directory:
            directory.publish(path.name, self.proofs[producer], new=not path.exists())

    def install(self):
        evidence.install_enrollment(self.state, self.config)
        for producer, entry in self.config['producers'].items():
            if entry['mode'] == 'ledger':
                evidence.initialize_producer(self.state, producer, now_ms=NOW)

    def aggregate(self, **kwargs):
        return evidence.aggregate(self.state, now_ms=kwargs.get('now_ms', NOW))

    def producer(self, name='start_canary', epoch='epoch-1'):
        return evidence.Producer(self.state, name, epoch)

    def unavailable(self):
        result = self.aggregate()
        self.assertEqual(result['kind'], 'hub_alert_evidence_unavailable_v1')
        self.assertNotIn('count', result)
        self.assertNotIn('observed_at_ms', result)

    def test_remote_kind_is_bound_to_exact_supported_producer_and_external_mode(self):
        kinds = {'credential_monitor':'credential_monitor_post_v1',
                 'picker_excluded':'picker_diagnostic_v1', 'picker_quota_stale':'picker_diagnostic_v1'}
        for name, kind in kinds.items():
            entry = {'mode':'external','remote':{'kind':kind,'base_url':'https://hub.example.invalid',
                     'credential_path':str(self.temp/'not-read.json'),'source_id':'a'*64,'release_sha':'b'*40}}
            self.assertEqual(evidence.remote_spec(name, entry), entry['remote'])
            for bad_name in ('picker_control','start_canary','quota_stale','backup','unknown'):
                with self.subTest(name=name, bad_name=bad_name),self.assertRaises(evidence.EvidenceUnavailable):
                    evidence.remote_spec(bad_name, entry)
            for patch in ({'mode':'ledger'}, {'witness_path':str(self.temp/'cached-zero.json')}):
                with self.subTest(patch=patch),self.assertRaises(evidence.EvidenceUnavailable):
                    evidence.remote_spec(name, dict(entry, **patch))
            remote = dict(entry['remote'], kind='picker_diagnostic_v1' if name=='credential_monitor' else 'credential_monitor_post_v1')
            with self.assertRaises(evidence.EvidenceUnavailable):
                evidence.remote_spec(name, dict(entry, remote=remote))

    def test_missing_state_is_unknown_and_readonly_without_zero_enrollment(self):
        self.unavailable()
        self.assertFalse(self.state.exists())
        bad = copy.deepcopy(self.config)
        bad['producers'].pop('quota_stale')
        with self.assertRaises(evidence.EvidenceUnavailable):
            evidence.install_enrollment(self.state, bad)
        self.assertFalse(self.state.exists())

    def test_explicit_baseline_and_complete_independent_sources_emit_exact_contract(self):
        self.install()
        before = sorted(p.name for p in self.state.iterdir())
        result = self.aggregate()
        self.assertEqual(result['kind'], 'dev_alert_occurrences_v1')
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['recipient'], 'dev@dtcdev.click')
        self.assertEqual(len(result['generation']), 64)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), before)

    def test_real_acceptance_driver_accepts_only_complete_fresh_aggregate(self):
        spec = importlib.util.spec_from_file_location('alert_evidence_acceptance',
                                                    ROOT / 'scripts/hub_acceptance_10.py')
        acceptance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(acceptance)
        self.install()
        runner = mock.Mock()
        runner.json.return_value = self.aggregate()
        before = acceptance.sample_alerts(runner, wall_clock=lambda: NOW / 1000)
        invocation = self.producer().begin('real-driver-decision')
        invocation.record('deduplicated-decision', 'alert_dedup')
        invocation.complete()
        self.write_proof('start_canary', started_total=1, finished_total=1)
        runner.json.return_value = self.aggregate()
        after = acceptance.sample_alerts(runner, wall_clock=lambda: NOW / 1000)
        self.assertEqual(acceptance.alert_changes(before, after),
                         ['dev alert occurrences during run: 1'])
        self.write_proof('start_canary', complete=False)
        runner.json.return_value = self.aggregate()
        with self.assertRaisesRegex(RuntimeError, 'watermark required'):
            acceptance.sample_alerts(runner, wall_clock=lambda: NOW / 1000)

    def test_rebaseline_cannot_hide_occurrence_from_real_acceptance_driver(self):
        spec = importlib.util.spec_from_file_location('alert_evidence_rebaseline_acceptance',
                                                    ROOT / 'scripts/hub_acceptance_10.py')
        acceptance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(acceptance)
        self.install()
        runner = mock.Mock()

        def sample():
            runner.json.return_value = self.aggregate()
            return acceptance.sample_alerts(runner, wall_clock=lambda: NOW / 1000)

        before = sample()
        invocation = self.producer().begin('invocation-1')
        invocation.record('actual-alert', 'alert')
        invocation.complete()
        self.write_proof('start_canary', started_total=1, finished_total=1)
        self.assertEqual(sample()['count'], 1)
        # Explicit owner recovery, using only this test's own private files,
        # can reset the baseline but cannot retain the acceptance generation.
        (self.state / 'start_canary.json').unlink()
        self.write_proof('start_canary', baseline_invocations=1, baseline_count=0)
        evidence.initialize_producer(self.state, 'start_canary', now_ms=NOW)
        after = sample()
        self.assertEqual(after['count'], before['count'])
        self.assertEqual(acceptance.alert_changes(before, after),
                         ['alert watermark generation changed'])

        # External sources must obey the same baseline boundary contract.
        before = after
        self.write_proof('picker_control', occurrence_count=1)
        self.assertEqual(sample()['count'], 1)
        self.write_proof('picker_control', started_total=1, finished_total=1,
                         baseline_invocations=1, occurrence_count=0)
        after = sample()
        self.assertEqual(after['count'], before['count'])
        self.assertEqual(acceptance.alert_changes(before, after),
                         ['alert watermark generation changed'])
        before = after
        self.write_proof('picker_control', baseline_count=1, occurrence_count=1)
        self.assertNotEqual(sample()['generation'], before['generation'])

    def test_delivery_mode_and_witness_configuration_change_generation(self):
        self.install()
        before = self.aggregate()['generation']
        entry = self.config['producers']['start_canary']
        entry['mode'] = 'external'
        with evidence.PrivateDirectory(self.state) as directory:
            directory.publish('enrollment.json', self.config)
        after = self.aggregate()['generation']
        self.assertNotEqual(after, before)
        alternate = self.controller / 'alternate-start-canary.json'
        with evidence.PrivateDirectory(self.controller) as directory:
            directory.publish(alternate.name, self.proofs['start_canary'], new=True)
        entry['witness_path'] = str(alternate)
        with evidence.PrivateDirectory(self.state) as directory:
            directory.publish('enrollment.json', self.config)
        self.assertNotEqual(self.aggregate()['generation'], after)

    def test_actual_decisions_before_dedup_and_recovery_count_but_observations_do_not(self):
        self.install()
        invocation = self.producer().begin('invocation-1')
        self.unavailable()
        for decision, kind in evidence.CANARY_DECISIONS.items():
            self.assertTrue(invocation.record('decision-' + decision, kind))
        self.assertFalse(invocation.record('decision-critical', 'alert'))
        invocation.complete()
        self.write_proof('start_canary', started_total=1, finished_total=1)
        self.assertEqual(self.aggregate()['count'], 3)
        # A later deduplicated occurrence is a NEW decision, not the incident ID.
        later = self.producer().begin('invocation-2')
        later.record('new-critical-dedup-decision', 'alert_dedup')
        later.complete()
        self.write_proof('start_canary', started_total=2, finished_total=2)
        self.assertEqual(self.aggregate()['count'], 4)

    def test_failed_begin_crash_and_later_success_never_paint_over_invocation_gap(self):
        self.install()
        with mock.patch.object(evidence.PrivateDirectory, 'publish', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.producer().begin('unrecorded-invocation')
        # The actual controller saw the invocation even though this journal did not.
        self.write_proof('start_canary', started_total=1, finished_total=1)
        self.unavailable()
        later = self.producer().begin('later-invocation')
        later.record('later-health', 'observation')
        later.complete()
        self.write_proof('start_canary', started_total=2, finished_total=2)
        self.unavailable()

    def test_crash_after_begin_leaves_durable_active_witness(self):
        self.install()
        self.producer().begin('abandoned')
        self.write_proof('start_canary', started_total=1, finished_total=1)
        self.unavailable()
        with self.assertRaises(evidence.EvidenceUnavailable):
            self.producer().begin('abandoned')

    def test_record_failure_latches_dirty_even_if_caller_continues_alert_path(self):
        self.install()
        invocation = self.producer().begin('invocation-1')
        delivered = []
        with mock.patch.object(evidence.PrivateDirectory, 'publish', side_effect=OSError('synthetic')):
            try:
                invocation.record('alert-1', 'alert')
            except OSError:
                pass  # Producer integration must continue its actual alert path.
            delivered.append('synthetic-alert-path-continued')
        self.assertTrue(delivered)
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, 'observation_failed'):
            invocation.complete()
        self.write_proof('start_canary', started_total=1, finished_total=1)
        self.unavailable()

    def test_published_but_unsynced_observation_is_dirty_and_never_completed(self):
        self.install()
        invocation = self.producer().begin('invocation-1')
        with mock.patch.object(evidence.PrivateDirectory, 'sync', side_effect=OSError('synthetic fsync')):
            with self.assertRaises(OSError):
                invocation.record('alert-1', 'alert')
        with self.assertRaises(evidence.EvidenceUnavailable):
            invocation.complete()
        self.write_proof('start_canary', started_total=1, finished_total=1)
        self.unavailable()

    def test_identity_replay_conflict_never_double_counts_or_clears_dirty(self):
        self.install()
        invocation = self.producer().begin('invocation-1')
        invocation.record('decision-1', 'alert')
        self.assertFalse(invocation.record('decision-1', 'alert'))
        with self.assertRaises(evidence.EvidenceUnavailable):
            invocation.record('decision-1', 'observation')
        with self.assertRaises(evidence.EvidenceUnavailable):
            invocation.complete()
        self.unavailable()

    def test_bounded_capacity_fails_unknown_instead_of_evicting_receipts(self):
        self.install()
        invocation = self.producer().begin('invocation-1')
        with mock.patch.object(evidence, 'MAX_RECEIPTS', 1):
            invocation.record('decision-1', 'alert')
            with self.assertRaisesRegex(evidence.EvidenceUnavailable, 'capacity'):
                invocation.record('decision-2', 'alert_dedup')
        self.unavailable()

    def test_epoch_restart_and_installed_source_changes_invalidate_coverage(self):
        self.install()
        initial = self.aggregate()['generation']
        self.write_proof('start_canary', epoch='epoch-2')
        self.unavailable()
        with self.assertRaises(evidence.EvidenceUnavailable):
            self.producer(epoch='epoch-2').begin('new-process')
        self.write_proof('start_canary', epoch='epoch-1')
        self.assertEqual(self.aggregate()['generation'], initial)
        self.artifact.write_text('# runtime was replaced\n')
        self.unavailable()

    def test_freshness_is_minimum_real_controller_barrier_not_current_wall_clock(self):
        self.install()
        self.write_proof('start_canary', observed_at_ms=NOW - 700)
        self.assertEqual(self.aggregate()['observed_at_ms'], NOW - 700)
        self.unavailable_at(NOW + evidence.MAX_AGE_MS)
        self.write_proof('start_canary', observed_at_ms=NOW + 5001)
        self.unavailable()

    def unavailable_at(self, now):
        self.assertEqual(self.aggregate(now_ms=now)['kind'], 'hub_alert_evidence_unavailable_v1')

    def test_external_picker_snapshot_requires_complete_independent_coverage_not_epoch_only(self):
        self.install()
        self.write_proof('picker_control', limitations=['b3_unobserved_draw_gap'])
        self.unavailable()
        self.write_proof('picker_control', limitations=[], occurrence_count=4)
        self.assertEqual(self.aggregate()['count'], 4)
        self.write_proof('picker_control', complete=False)
        self.unavailable()
        self.write_proof('picker_control', complete=True, baseline_count=5)
        self.unavailable()

    def test_missing_corrupt_and_counter_reset_are_unknown(self):
        self.install()
        invocation = self.producer().begin('invocation-1')
        invocation.record('decision-1', 'alert')
        invocation.complete()
        self.write_proof('start_canary', started_total=1, finished_total=1)
        with evidence.PrivateDirectory(self.state) as directory:
            record = directory.read('start_canary.json')
            record['count'] = 0
            directory.publish('start_canary.json', record)
        self.unavailable()
        (self.state / 'start_canary.json').write_text('{broken')
        self.unavailable()
        (self.state / 'start_canary.json').unlink()
        self.unavailable()

    def test_duplicate_coverage_and_state_keys_are_corrupt_not_last_key_wins(self):
        self.install()
        target = self.controller / 'picker_control.json'
        original = target.read_bytes()
        malformed = (b'{"occurrence_count":1,' + original[1:],
                     b'{"complete":false,' + original[1:],
                     original[:-1] + b',"extra":{"dirty":true,"dirty":false}}')
        for raw in malformed:
            with self.subTest(raw=raw):
                # Default json.loads would silently hide the earlier value.
                self.assertEqual(json.loads(raw)['occurrence_count'], 0)
                target.write_bytes(raw)
                self.unavailable()
        target.write_bytes(original)
        target = self.state / 'start_canary.json'
        original = target.read_bytes()
        target.write_bytes(b'{"count":1,' + original[1:])
        self.assertEqual(json.loads(target.read_bytes())['count'], 0)
        self.unavailable()

    def test_witness_counter_or_baseline_mismatch_is_unknown(self):
        self.install()
        for changes in ({'started_total': 1}, {'started_total': 1, 'finished_total': 1},
                        {'baseline_count': 9}, {'baseline_invocations': 1}):
            with self.subTest(changes=changes):
                original = dict(self.proofs['start_canary'])
                self.write_proof('start_canary', **changes)
                self.unavailable()
                self.proofs['start_canary'] = original
                self.write_proof('start_canary')

    def test_nonprivate_symlink_hardlink_and_fifo_state_fail_without_following(self):
        self.install()
        target = self.state / 'quota_stale.json'
        original = target.read_bytes()
        victim = self.temp / 'owned-victim'
        victim.write_bytes(original)
        victim.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'fifo', 'nonprivate'):
            with self.subTest(kind=kind):
                target.unlink()
                if kind == 'symlink': target.symlink_to(victim)
                elif kind == 'hardlink': os.link(victim, target)
                elif kind == 'fifo': os.mkfifo(target, 0o600)
                else:
                    target.write_bytes(original)
                    target.chmod(0o644)
                began = time.monotonic()
                self.unavailable()
                self.assertLess(time.monotonic() - began, 1)
                self.assertEqual(victim.read_bytes(), original)

    def test_state_ancestor_symlink_and_world_writable_directory_are_refused(self):
        self.install()
        link = self.temp / 'link'
        link.symlink_to(self.state, target_is_directory=True)
        self.assertEqual(evidence.aggregate(link, now_ms=NOW)['kind'], 'hub_alert_evidence_unavailable_v1')
        self.state.chmod(0o755)
        self.unavailable()

    def test_lock_wait_is_bounded_and_aggregate_does_not_create_a_missing_lock(self):
        self.install()
        with evidence.PrivateDirectory(self.state) as directory, directory.lock(write=True):
            began = time.monotonic()
            self.unavailable()
            self.assertLess(time.monotonic() - began, 1)
        (self.state / 'ledger.lock').unlink()
        self.unavailable()
        self.assertFalse((self.state / 'ledger.lock').exists())

    def test_failed_directory_creation_sync_is_repaired_or_refused_on_retry(self):
        with mock.patch.object(evidence.os, 'fsync', side_effect=OSError('synthetic sync')):
            with self.assertRaises(OSError):
                evidence.PrivateDirectory(self.state, create=True)
        # The path may remain, but a second attempt still performs the full sync.
        with mock.patch.object(evidence.os, 'fsync', side_effect=OSError('still failing')):
            with self.assertRaises(OSError):
                evidence.PrivateDirectory(self.state, create=True)
        self.unavailable()


if __name__ == '__main__':
    unittest.main()
