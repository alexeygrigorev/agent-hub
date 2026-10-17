"""Explicit capture retention survives retirement without inventing a transcript."""
import dataclasses
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import capture_evidence as ce, session_executor as se
from agent_hub_host.native_sessions import NativeTarget, SessionControlError


class CaptureRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.journal = str(self.base / 'journal')
        Path(self.journal).mkdir(mode=0o700)
        self.target = NativeTarget(str(uuid.uuid4()), 'owned', str(self.base.resolve()),
                                   created_at_ms=1000, engine='antigravity')
        self.capture = {'text': 'actual last available output\n', 'mode': 'screen', 'truncated': False}

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, capture=None):
        with patch.object(ce.time, 'time', return_value=2):
            return ce.retain_capture(self.journal, self.target, 'host', 'actual-command', capture or self.capture)

    def read(self, target=None, machine='host', finished=3000):
        with patch.object(ce.time, 'time', return_value=4):
            return ce.read_retained_capture(self.journal, target or self.target, machine, finished)

    def test_retains_actual_bytes_identity_and_original_time(self):
        self.assertTrue(self.store())
        raw, meta = self.read()
        self.assertEqual(raw.decode(), self.capture['text'])
        self.assertEqual(meta['captured_at_ms'], 2000)
        self.assertEqual(meta['command_id'], 'actual-command')
        self.assertEqual(meta['binding']['created_at_ms'], 1000)
        for file in Path(self.journal).iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_missing_or_empty_capture_does_not_manufacture_history(self):
        with self.assertRaises(SessionControlError): self.read()
        self.assertFalse(self.store({**self.capture, 'text': ''}))
        with self.assertRaises(SessionControlError): self.read()

    def test_exact_host_and_incarnation_are_required(self):
        self.store()
        for target, machine in [(dataclasses.replace(self.target, created_at_ms=1001), 'host'),
                                (self.target, 'other'),
                                (dataclasses.replace(self.target, tag='reused'), 'host')]:
            with self.subTest(target=target, machine=machine), self.assertRaises(SessionControlError):
                self.read(target=target, machine=machine)

    def test_corruption_partial_write_and_chronology_refuse(self):
        self.store()
        for finished in (1999, True, 5000):
            with self.subTest(finished=finished), self.assertRaises(SessionControlError):
                self.read(finished=finished)
        payload = next(Path(self.journal).glob('*.bin'))
        payload.write_bytes(b'changed')
        with self.assertRaises(SessionControlError): self.read()
        payload.unlink()
        with self.assertRaises(SessionControlError): self.read()

    def test_latest_capture_replaces_bounded_payload(self):
        self.store()
        self.store({**self.capture, 'text': 'x' * (ce.LIMIT + 20)})
        raw, meta = self.read()
        self.assertEqual(len(raw), ce.LIMIT)
        self.assertTrue(meta['truncated'])
        self.assertEqual(len(list(Path(self.journal).iterdir())), 3)

    def test_symlink_cache_payload_refuses(self):
        self.store()
        payload = next(Path(self.journal).glob('*.bin'))
        other = self.base / 'other'; other.write_bytes(b'outside')
        payload.unlink(); payload.symlink_to(other)
        with self.assertRaises(SessionControlError): self.read()

    def test_capture_operation_archives_and_retired_cleanup_preserves_provenance(self):
        custody = se.CallerCustody(actor='machine:host', machine_id='host', credential_family='family')
        with patch.object(se, 'capture_session_output', return_value=self.capture), \
                patch.object(ce.time, 'time', return_value=2):
            result = se._run_claimed_effect('unused', 'capture', {'command_id': 'actual-command', 'mode': 'screen'},
                                           self.target, custody=custody, journal_dir=self.journal)
        self.assertEqual(result['state'], 'captured')
        with patch.object(se, 'capture_session_output', side_effect=SessionControlError('retired')), \
                patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, {'finished_at_ms': 3000}, 'verified')), \
                patch('agent_hub_host.retired_native.capture_retired_history', return_value=(None, None, None, 'missing')), \
                patch.object(ce.time, 'time', return_value=4):
            path = se._preserve_cleanup_evidence('unused', self.target, custody=custody,
                                                 journal_dir=self.journal, command_id='cleanup-command')
        record = json.loads(Path(path).read_text())
        self.assertEqual(record['capture_observed_at_ms'], 2000)
        self.assertEqual(record['native_finished_at_ms'], 3000)
        self.assertEqual(record['evidence_observed_at_ms'], 4000)
        self.assertEqual(record['capture_command_id'], 'actual-command')
        self.assertEqual(record['retired_source'], 'commanded-capture')
        self.assertEqual(Path(self.journal, record['raw_file']).read_bytes(), self.capture['text'].encode())

    def test_uncertain_native_retirement_cannot_use_archive(self):
        self.store()
        custody = se.CallerCustody(actor='machine:host', machine_id='host', credential_family='family')
        with patch.object(se, 'capture_session_output', side_effect=SessionControlError('absent')), \
                patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(False, None, 'unknown')):
            with self.assertRaises(SessionControlError):
                se._preserve_cleanup_evidence('unused', self.target, custody=custody,
                                             journal_dir=self.journal, command_id='cleanup-command')
