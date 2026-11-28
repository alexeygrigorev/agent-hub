"""Intake 62ccfa1e: a native that refused to start on its engine auth check is named."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_hub_host import agent
from agent_hub_host import task_output

CHECK = ('{"event": "opencode_auth_target_reimport", "step": "model_list", "failure": "model_unavailable"}\n'
         '{"event": "opencode_auth_check_unavailable", "reason": "engine_auth_check_unavailable", '
         '"step": "model_list", "failure": "model_unavailable"}\n')


class EnginePreflightOutputTest(unittest.TestCase):
    def test_bootstrap_event_lines_map_to_closed_reasons(self):
        self.assertEqual(agent.engine_preflight_failure(CHECK), 'engine_auth_check_unavailable')
        self.assertEqual(agent.engine_preflight_failure(
            '{"event": "opencode_auth_expired", "reason": "engine_auth_expired"}\n'), 'engine_auth_expired')
        for reason in ('engine_auth_check_unavailable', 'engine_auth_expired'):
            self.assertIn(reason, agent.HOST_FAILURE_DETAILS)

    def test_claude_auth_expired_banners_map_to_engine_auth_expired(self):
        for line in (
            'Failed to authenticate: OAuth session expired and could not be refreshed\n',
            'Not logged in · Please run /login\n',
            'Login expired · Please run /login\n',
        ):
            self.assertEqual(agent.engine_preflight_failure(line), 'engine_auth_expired', line)

    def test_other_output_is_not_a_preflight_failure(self):
        for output in ('', None, 'Traceback: AuthCheckUnavailable\n', '{"event": "fleet_device_auth"}\n',
                       'agent said {"event": "opencode_auth_expired"}\n', '{"event": broken\n',
                       json.dumps({'type': 'text', 'text': '{"event": "opencode_auth_expired"}'}) + '\n'):
            self.assertIsNone(agent.engine_preflight_failure(output), output)


class EnginePreflightRefusalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.row = {'workspace': self.dir.name, 'session_id': 'sess-1', 'operation_id': 'op-1', 'epoch': 2,
                    'native_id': '11111111-2222-3333-4444-555555555555', 'tag': 'task-hub2-sess-1'}
        self.frames = []
        self.bridge = types.SimpleNamespace(_managed_socket=types.SimpleNamespace(send=self.frames.append))

    def refusal(self, output):
        with mock.patch.object(task_output, 'capture_task_output', return_value=output) as capture:
            reason = agent.Bridge._engine_preflight_refusal(self.bridge, self.row)
        return reason, capture

    def test_auth_check_refusal_is_typed_and_reported_loudly(self):
        reason, capture = self.refusal(CHECK)
        self.assertEqual(reason, 'engine_auth_check_unavailable')
        self.assertEqual(capture.call_args.args[0], 'hub2-sess-1')
        self.assertTrue(capture.call_args.kwargs['retained_only'])
        frame = json.loads(self.frames[0])
        self.assertEqual({k: frame[k] for k in ('type', 'stage', 'reason', 'session_id', 'operation_id', 'epoch')},
                         {'type': 'host.error', 'stage': 'host_runtime', 'reason': 'engine_auth_check_unavailable',
                          'session_id': 'sess-1', 'operation_id': 'op-1', 'epoch': 2})
        self.assertEqual(frame['detail'], agent.HOST_FAILURE_DETAILS['engine_auth_check_unavailable'])

    def test_a_worker_that_proved_it_was_live_is_never_a_preflight_refusal(self):
        (Path(self.dir.name) / '.local').mkdir()
        (Path(self.dir.name) / '.local' / 'first-action-hub2-sess-1.json').write_text('{}')
        reason, capture = self.refusal(CHECK)
        self.assertIsNone(reason)
        capture.assert_not_called()
        self.assertEqual(self.frames, [])

    def test_unavailable_output_is_not_evidence(self):
        with mock.patch.object(task_output, 'capture_task_output', side_effect=RuntimeError('task_output_unavailable')):
            self.assertIsNone(agent.Bridge._engine_preflight_refusal(self.bridge, self.row))
        self.assertEqual(self.frames, [])


if __name__ == '__main__':
    unittest.main()
