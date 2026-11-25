"""The native receipt never waits for first-action proof; proof is a later activity update."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host import first_action_observation as fao
from agent_hub_host import native_launch as nl

SESSION = '11111111-2222-3333-4444-555555555555'
TASK = 'hub2-' + SESSION
NATIVE = 'd6cf9872-8597-48a5-be64-990cdef4db31'


class ImmediateReceiptThenLateProofTests(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.addCleanup(os.umask, self.umask)
        self.directory = tempfile.mkdtemp(prefix='nl-', dir=os.environ.get('TMPDIR') or None)
        self.addCleanup(__import__('shutil').rmtree, self.directory, True)
        self.workspace = str(Path(self.directory).resolve())
        self.created = int(time.time() * 1000) - 5000
        prompt = 'echo hello'
        self.child = ['/bin/sh', '-c', prompt]
        self.start = {'id': NATIVE, 'tag': 'task-' + TASK, 'workspace': self.workspace, 'command': self.child,
                      'phase': 'starting', 'schema_version': 1, 'created_at_ms': self.created}
        self.runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout=json.dumps(self.start)))
        self.adapter = nl.NativeLauncher({'routes': {'shell': {'provider': 'shell'}}}, runner=self.runner)
        self.spec = {'workspace': self.workspace, 'engine': 'shell', 'model': 'shell', 'prompt': prompt,
                     'interactive': False, 'timeout_seconds': 60, 'operation_id': 'operation-1', 'epoch': 1}
        self.candidate = {'session_id': SESSION, 'operation_id': 'operation-1', 'epoch': 1, 'native_id': NATIVE,
                          'tag': 'task-' + TASK, 'workspace': self.workspace, 'created_at_ms': self.created}

    def launch(self):
        # A launch that blocked on proof would never return: the polling wait is not reachable.
        with mock.patch.object(self.adapter, 'wait_first_action', side_effect=AssertionError('blocking wait')), \
                mock.patch.object(nl.time, 'sleep', side_effect=AssertionError('blocking sleep')):
            return self.adapter.launch(TASK, dict(self.spec))

    def whoami(self, stamp):
        return {'id': NATIVE, 'tag': self.start['tag'], 'workspace': self.workspace, 'command': ['x'],
                'phase': 'working', 'schema_version': 2, 'worker_pid': os.getpid() + 1, 'updated_at_ms': stamp}

    def stored(self):
        return json.loads(fao.record_path(self.workspace, TASK).read_text())

    def test_receipt_is_immediate_and_late_proof_is_published_once_afterwards(self):
        begin = time.monotonic()
        receipt = self.launch()
        self.assertLess(time.monotonic() - begin, 5)
        self.assertEqual(receipt['session_id'], NATIVE)
        self.assertIs(receipt['first_action_verified'], False)
        stored = self.stored()
        self.assertIsNone(stored['first_action_validated_at'])
        self.assertEqual((stored['operation_id'], stored['epoch']), ('operation-1', 1))
        self.assertEqual(stored['native_start']['id'], NATIVE)
        self.assertIsInstance(stored['native_dispatch_at'], float)
        # The model's tool work lands later; the observer publishes it exactly once.
        artifact = fao.artifact_path(self.workspace, TASK)
        artifact.write_text(json.dumps(self.whoami(self.created + 3000)))
        os.chmod(artifact, 0o644)
        late = fao.LateFirstAction()
        late.observe(self.candidate)
        self.assertEqual(late.activity(self.candidate), self.created + 3000)
        proven = self.stored()
        self.assertEqual(proven['first_action_activity_ms'], self.created + 3000)
        self.assertAlmostEqual(proven['first_action_validated_at'], artifact.stat().st_mtime, delta=1)
        self.assertEqual(proven['native_started_at'], stored['native_started_at'])
        self.assertEqual(self.adapter.recover.__func__, nl.NativeLauncher.recover)

    def test_immediate_proof_keeps_artifact_time_and_receipt_stays_truthful(self):
        artifact = fao.artifact_path(self.workspace, TASK)
        artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact.write_text(json.dumps(self.whoami(self.created + 2000)))
        os.chmod(artifact, 0o644)
        past = time.time() - 1
        os.utime(artifact, (past, past))
        receipt = self.launch()
        self.assertIs(receipt['first_action_verified'], True)
        stored = self.stored()
        self.assertAlmostEqual(stored['first_action_validated_at'], past, delta=0.5)
        self.assertEqual(stored['first_action_activity_ms'], self.created + 2000)

    def test_symlinked_immediate_artifact_is_not_proof(self):
        real = Path(self.directory) / 'foreign.json'
        real.write_text(json.dumps(self.whoami(self.created + 2000)))
        artifact = fao.artifact_path(self.workspace, TASK)
        artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact.symlink_to(real)
        self.assertIs(self.launch()['first_action_verified'], False)
        self.assertIsNone(self.stored()['first_action_validated_at'])

    def test_retry_replaces_evidence_and_old_proof_cannot_leak_into_it(self):
        self.launch()
        artifact = fao.artifact_path(self.workspace, TASK)
        artifact.write_text(json.dumps(self.whoami(self.created + 3000)))
        os.chmod(artifact, 0o644)
        late = fao.LateFirstAction()
        late.observe(self.candidate)
        self.assertIsNotNone(self.stored()['first_action_validated_at'])
        # Retry under a new fence and birth rewrites the evidence unlatched.
        self.spec['epoch'] = 2
        self.start['created_at_ms'] = self.created + 100000
        self.runner.return_value = mock.Mock(returncode=0, stdout=json.dumps(self.start))
        self.launch()
        stored = self.stored()
        self.assertEqual(stored['epoch'], 2)
        self.assertIsNone(stored['first_action_validated_at'])
        retry = {**self.candidate, 'epoch': 2, 'created_at_ms': self.created + 100000}
        fresh = fao.LateFirstAction()
        fresh.observe(retry)  # the previous incarnation's artifact is not this one's proof
        self.assertIs(fresh.activity(retry), fao.UNOBSERVED)
        self.assertIsNone(self.stored()['first_action_validated_at'])


if __name__ == '__main__':
    unittest.main()
