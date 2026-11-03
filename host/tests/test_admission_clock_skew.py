"""Bounded clock skew must not extend admission's stale or execution window."""
import datetime as dt
import unittest
from unittest.mock import patch
from agent_hub_host import fleet
from agent_hub_host.dispatch_diagnostics import AdmissionRefusedError, classify


class AdmissionClockSkewTests(unittest.TestCase):
    def dispatch(self, age, duration=1):
        now = dt.datetime.now(dt.timezone.utc)
        at = (now - dt.timedelta(seconds=age)).isoformat()
        gate = dict(launch_allowed=True, profile_supported=True,
                    account_label='shared-codex', observed_at=at, gate_post_stamp_s=0.4,
                    server_observed_at=(now + dt.timedelta(seconds=1.7)).isoformat())
        dispatcher = fleet.Dispatcher({'version': 1, 'workspaces': {}, 'subscriptions': []})
        with patch.object(dispatcher, '_admission_gate', return_value=gate), \
                patch.object(fleet.time, 'monotonic', side_effect=[0, duration]):
            return dispatcher._dispatch_admission({'account_label': 'shared-codex'}, {}, None, {})

    def test_negative_zero_and_fresh_age(self):
        for age in (-1.7, -4.9, 0, 29):
            with self.subTest(age=age):
                self.assertTrue(self.dispatch(age))

    def test_stale_future_and_slow_gate_refused_with_numeric_evidence(self):
        for age, duration in ((30.1, 1), (-5.1, 1), (0, 30.1)):
            with self.subTest(age=age, duration=duration):
                with self.assertRaises(AdmissionRefusedError) as caught:
                    self.dispatch(age, duration)
                diagnostic = classify(caught.exception)
                self.assertEqual(diagnostic['gate_reason'], 'admission_timestamp_stale')
                self.assertAlmostEqual(diagnostic['admission_age_s'], age, delta=0.1)
                self.assertEqual(diagnostic['gate_duration_s'], duration)
                self.assertEqual(diagnostic['gate_post_stamp_s'], 0.4)
                self.assertAlmostEqual(diagnostic['host_minus_hub_s'], -1.7, places=1)

    def test_timing_redacts_arbitrary_nonfinite_and_unbounded_values(self):
        diagnostic = classify(AdmissionRefusedError('Fresh admission timestamp required',
            'admission_timestamp_stale', timing={'secret': 'private', 'admission_age_s': 'private',
            'host_minus_hub_s': float('nan'), 'gate_duration_s': 999999}))
        self.assertNotIn('secret', diagnostic)
        self.assertNotIn('admission_age_s', diagnostic)
        self.assertNotIn('host_minus_hub_s', diagnostic)
        self.assertNotIn('gate_duration_s', diagnostic)
