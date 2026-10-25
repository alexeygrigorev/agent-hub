import json
import unittest
from unittest.mock import patch
from agent_hub_host.runtime_diagnostics import runtime_diagnostic

NATIVE = '2505c5ae-605e-426a-8908-75a293bdc4c8'
RECORD = dict(native_id=NATIVE, incarnation=1000000)
TOMB = dict(finished_at_ms=3000000)
UNIT = 'aplexer-workload-' + NATIVE + '.scope'

def log(message, stamp=2000000000):
    return json.dumps(dict(MESSAGE=message, __REALTIME_TIMESTAMP=str(stamp)))

class RuntimeDiagnostics(unittest.TestCase):
    def test_real_systemd_oom_is_runtime_not_dispatch_and_never_copies_logs(self):
        result = runtime_diagnostic(RECORD, TOMB, query=lambda a: log(UNIT + ': A process of this unit has been killed by the OOM killer.'))
        self.assertEqual(result['reason'], 'engine_oom_killed')
        self.assertEqual(result['stage'], 'runtime')
        self.assertNotIn(NATIVE, json.dumps(result))

    def test_unit_and_incarnation_are_fenced_and_arbitrary_text_withheld(self):
        for raw in [log('other.scope: A process of this unit has been killed by the OOM killer.'),
                    log(UNIT + ': A process of this unit has been killed by the OOM killer.', 999000000),
                    log('secret=private OOM killer'), 'not json', 'x' * 65537]:
            with self.subTest(raw=raw[:80]):
                result = runtime_diagnostic(RECORD, TOMB, query=lambda a: raw)
                self.assertEqual(result['reason'], 'engine_exited_without_result')
                self.assertNotIn('secret', json.dumps(result))

    def test_native_exit_code_signal_and_timeout(self):
        for info, reason in [({'code': 17}, 'engine_exit_code'), ({'signal': 9}, 'engine_signal_exit'),
                             ({'oom_killed': True, 'signal': 9}, 'engine_oom_killed')]:
            result = runtime_diagnostic(dict(RECORD, runtime_exit=info), TOMB, query=lambda a: '')
            self.assertEqual(result['reason'], reason)
        result = runtime_diagnostic(RECORD, TOMB, query=lambda a: log(UNIT + ': Runtime time limit exceeded. Stopping.'))
        self.assertEqual(result['reason'], 'engine_runtime_timeout')

    def test_journal_unavailable_keeps_honest_unknown(self):
        with patch('agent_hub_host.runtime_diagnostics.subprocess.run', side_effect=OSError):
            self.assertEqual(runtime_diagnostic(RECORD, TOMB)['reason'], 'engine_exited_without_result')
        self.assertIsNone(runtime_diagnostic(dict(RECORD, native_id='bad/../unit'), TOMB))
        self.assertIsNone(runtime_diagnostic(RECORD, {}))
