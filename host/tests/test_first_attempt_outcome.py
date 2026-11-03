import json
import unittest

from agent_hub_host.first_attempt_outcome import (
    berlin_day_range,
    build_first_attempt_outcomes,
    classify,
    engine_family,
    is_qualifying,
    normalize_reason,
    report_for_day,
    report_state,
    stamp_launch_record,
)

INTAKE_A = '11111111-1111-4111-8111-111111111111'
S_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
S_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
S_C = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
S_D = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
S_E = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
S_F = 'ffffffff-ffff-4fff-8fff-ffffffffffff'

# 2026-10-27 Berlin window is UTC 2026-10-26 23:00 .. 2026-10-27 23:00.
DAY = '2026-10-27'
T0 = 1793055600000  # 2026-10-26T23:00:00Z


def session(sid, intake=INTAKE_A, **fields):
    row = dict(id=sid, intake_id=intake, created_at=T0 + 1000,
               updated_at=T0 + 2000, engine='opencode', mode='headless',
               state='stopped')
    row.update(fields)
    return row


class FirstAttemptOutcome(unittest.TestCase):
    def test_dispatch_failure_case(self):
        row = session(S_A, state='failed',
                      diagnostic={'reason': 'dispatch_local_command_failed'})
        self.assertTrue(is_qualifying(row))
        code, stage, cause = classify(row)
        self.assertEqual((code, stage, cause),
                         ('dispatch_local_command_failed', 'dispatch_route', 'dispatch_failure'))

    def test_headless_exit_case(self):
        row = session(S_B, state='failed',
                      runtime={'reason': 'engine_exit_code', 'exit_code': 1},
                      finish_report='invalid_or_missing')
        self.assertTrue(is_qualifying(row))
        code, stage, cause = classify(row)
        self.assertEqual((code, stage, cause),
                         ('engine_exit_code', 'runtime', 'headless_exit'))

    def test_model_unsupported_case(self):
        row = session(S_C, state='refused',
                      reason='model_unsupported: maintained adapter refused requested model')
        self.assertTrue(is_qualifying(row))
        code, stage, cause = classify(row)
        self.assertEqual((code, stage, cause),
                         ('model_unsupported', 'dispatch_route', 'model_unsupported'))

    def test_hung_call_case(self):
        row = session(S_D, stall_reason='hung_tool_call', finish_report='valid_report_seen')
        self.assertTrue(is_qualifying(row))
        code, stage, cause = classify(row)
        self.assertEqual((code, stage, cause), ('hung_tool_call', 'stall', 'hung_call'))

    def test_missing_report_case(self):
        row = session(S_E, worker_report_received=False)
        self.assertTrue(is_qualifying(row))
        code, stage, cause = classify(row)
        self.assertEqual((code, stage, cause), ('missing_report', 'report', 'missing_report'))
        self.assertEqual(report_state(row), 'invalid_or_missing')

    def test_clean_stop_with_report_does_not_qualify(self):
        row = session(S_F, worker_report_received=True, finish_report='valid_report_seen')
        self.assertFalse(is_qualifying(row))

    def test_waiting_wake_is_not_a_failure(self):
        row = session(S_F, state='running', stalled=True, stall_reason='waiting_until',
                      waiting_until='2026-10-27T10:00:00+01:00')
        self.assertFalse(is_qualifying(row))

    def test_retry_success_preserves_initial_cause(self):
        first = session(S_A, state='failed', created_at=T0 + 1000,
                        diagnostic={'reason': 'dispatch_local_command_failed'})
        retry = session(S_B, state='stopped', created_at=T0 + 5000,
                        worker_report_received=True, finish_report='valid_report_seen')
        outcomes = build_first_attempt_outcomes([retry, first])
        self.assertEqual(len(outcomes), 1)
        row = outcomes[0]
        self.assertEqual(row['session_id'], S_A)
        self.assertEqual(row['attempt_index'], 1)
        self.assertEqual(row['group_size'], 2)
        self.assertEqual(row['reason_code'], 'dispatch_local_command_failed')
        self.assertEqual(row['retry_session_ids'], [S_B])
        self.assertEqual(row['retry_states'], ['stopped'])

    def test_no_secrets_or_raw_text_in_outcome(self):
        secret = 'ghp_' + 'x' * 20
        row = session(S_A, state='failed',
                      diagnostic={'reason': 'dispatch_local_command_failed'},
                      runtime={'tail': 'token=' + secret},
                      report='Bearer ' + secret)
        outcomes = build_first_attempt_outcomes([row])
        blob = json.dumps(outcomes)
        self.assertNotIn(secret, blob)
        self.assertNotIn('token=', blob)
        self.assertNotIn('Bearer', blob)

    def test_unknown_reason_collapses_to_other_without_raw_text(self):
        row = session(S_A, state='failed', reason='something entirely novel happened: kaboom')
        code, _stage, cause = classify(row)
        # Bare lifecycle fallback keeps the cause bucket honest without
        # forwarding the novel raw text.
        self.assertEqual(code, 'state_failed')
        self.assertEqual(cause, 'other')
        outcomes = build_first_attempt_outcomes([row])
        self.assertNotIn('kaboom', json.dumps(outcomes))
        self.assertNotIn('novel', json.dumps(outcomes))

    def test_engine_family_mapping(self):
        self.assertEqual(engine_family('zcodex'), 'codex')
        self.assertEqual(engine_family('OpenCode'), 'opencode')
        self.assertEqual(engine_family('antigravity'), 'antigravity')
        self.assertEqual(engine_family('mystery-engine'), 'unknown')
        self.assertEqual(engine_family(None), 'unknown')
        self.assertEqual(normalize_reason(None), 'other')

    def test_berlin_day_report_counts_and_examples(self):
        intakes = ['10000000-0000-4000-8000-%012d' % n for n in range(6)]
        rows = [
            session(S_A, intake=intakes[0], state='failed', created_at=T0 + 1000,
                    diagnostic={'reason': 'dispatch_local_command_failed'}),
            session(S_B, intake=intakes[1], state='failed', created_at=T0 + 2000,
                    runtime={'reason': 'engine_exit_code'}),
            session(S_C, intake=intakes[2], state='refused', created_at=T0 + 3000,
                    reason='model_unsupported'),
            session(S_D, intake=intakes[3], created_at=T0 + 4000, stall_reason='hung_tool_call',
                    finish_report='valid_report_seen'),
            session(S_E, intake=intakes[4], created_at=T0 + 5000, worker_report_received=False),
            # Outside the window: must not be counted.
            session(S_F, intake=intakes[5], state='failed', created_at=T0 - 1000,
                    diagnostic={'reason': 'dispatch_local_command_failed'}),
        ]
        report = report_for_day(rows, DAY, examples_per_cause=1)
        self.assertEqual(report['day'], DAY)
        self.assertEqual(report['sessions_in_day'], 5)
        self.assertEqual(report['qualifying_first_attempts'], 5)
        counts = {c['cause']: c['count'] for c in report['causes']}
        self.assertEqual(counts, {'dispatch_failure': 1, 'headless_exit': 1,
                                 'model_unsupported': 1, 'hung_call': 1,
                                 'missing_report': 1})
        for entry in report['causes']:
            self.assertEqual(len(entry['examples']), 1)
            self.assertIn('session_id', entry['examples'][0])
        # Window is the Berlin day in UTC.
        self.assertEqual(report['window_utc']['start'], '2026-10-26T23:00:00+00:00')
        self.assertEqual(report['window_utc']['end'], '2026-10-27T23:00:00+00:00')

    def test_berlin_day_range_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            berlin_day_range('27-10-2026')
        with self.assertRaises(ValueError):
            berlin_day_range('2026-13-01')

    def test_stamp_launch_record_emits_safe_evidence(self):
        record = dict(offer_id='launch-1', state='failed', engine='opencode',
                      mode='headless', worker_report_received=False,
                      diagnostic={'reason': 'launcher_command_failed',
                                  'tail': 'sk-ant-secret-value'})
        stamp_launch_record(record)
        stamped = record['first_attempt_outcome']
        self.assertEqual(stamped['reason_code'], 'launcher_command_failed')
        self.assertEqual(stamped['stage'], 'launcher_run')
        self.assertEqual(stamped['cause'], 'dispatch_failure')
        self.assertEqual(stamped['report'], 'invalid_or_missing')
        # The stamp itself carries no free-form text from the record.
        self.assertNotIn('sk-ant-secret-value', json.dumps(stamped))

    def test_stamp_launch_record_skips_live_states_and_never_raises(self):
        record = dict(offer_id='launch-2', state='running')
        self.assertIs(stamp_launch_record(record), record)
        self.assertNotIn('first_attempt_outcome', record)
        # Garbage in, same object out, no exception.
        self.assertIsNone(stamp_launch_record(None))


if __name__ == '__main__':
    unittest.main()
