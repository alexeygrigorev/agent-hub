"""Required CLI-glob bridge for the maintained CPU/resource contract tests."""
import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest


EXPECTED_CASE_IDS = (
    'retained_cpu_resource_contracts.CpuResourcesTests.test_cpu_admission_sanitizer_accepts_io_psi',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_cross_repository_wire_fixture',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_first_reset_missing_identity_and_out_of_order_are_unknown',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_iowait_and_guest_not_counted_as_extra_busy',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_lifetime_percentile_is_time_weighted_and_survives_journal_roundtrip',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_missing_io_pressure_reads_as_null_not_failure',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_peak_journal_and_final_report_keep_lifetime_p95',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_proc_stat_guest_fields_and_heartbeat_shape',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_refusals_preserve_only_fixed_cpu_reason',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_structured_refusal_numbers_are_bounded_and_private_text_is_rejected',
    'retained_cpu_resource_contracts.CpuResourcesTests.test_terminal_distribution_freezes_but_live_idle_intervals_remain_real',
    'retained_cpu_resource_contracts.WindowedCpuTests.test_window_history_is_bounded',
    'retained_cpu_resource_contracts.WindowedCpuTests.test_window_needs_a_snapshot_at_least_10_seconds_old',
    'retained_cpu_resource_contracts.WindowedCpuTests.test_windows_system_times_are_measured',
)
WINDOWS_CASE_ID = 'retained_cpu_resource_contracts.WindowedCpuTests.test_windows_system_times_are_measured'
WINDOWS_SKIP_REASON = 'Windows only'


def case_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from case_ids(item)
        else:
            yield item.id()


def require_cpu_result(checker, identities, result, *, windows, output):
    checker.assertEqual(sorted(identities), sorted(EXPECTED_CASE_IDS), output)
    checker.assertGreater(len(identities), 0, 'CPU resource contract collection must not be empty')
    checker.assertEqual(result.testsRun, len(EXPECTED_CASE_IDS), output)
    observed_skips = [(case.id(), reason) for case, reason in result.skipped]
    expected_skips = [] if windows else [(WINDOWS_CASE_ID, WINDOWS_SKIP_REASON)]
    checker.assertEqual(observed_skips, expected_skips, output)
    checker.assertTrue(result.wasSuccessful(), output)


class TestCurrentCpuResourceContracts(unittest.TestCase):
    def test_current_cpu_resource_contracts(self):
        source = Path(__file__).resolve().with_name('test_cpu_resources.py')
        spec = importlib.util.spec_from_file_location('retained_cpu_resource_contracts', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite = unittest.TestSuite([
            unittest.defaultTestLoader.loadTestsFromTestCase(module.WindowedCpuTests),
            unittest.defaultTestLoader.loadTestsFromTestCase(module.CpuResourcesTests),
        ])
        identities = list(case_ids(suite))
        self.assertEqual(suite.countTestCases(), len(EXPECTED_CASE_IDS))
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
        require_cpu_result(self, identities, result, windows=os.name == 'nt', output=output.getvalue())


class TestCpuResourceBridgeBoundary(unittest.TestCase):
    def result(self, skips=(), *, count=14, success=True):
        return SimpleNamespace(testsRun=count,
            skipped=[(SimpleNamespace(id=lambda identity=identity: identity), reason)
                     for identity, reason in skips], wasSuccessful=lambda: success)

    def test_only_original_platform_skip_is_accepted(self):
        require_cpu_result(self, EXPECTED_CASE_IDS, self.result(), windows=True, output='synthetic')
        require_cpu_result(self, EXPECTED_CASE_IDS,
            self.result([(WINDOWS_CASE_ID, WINDOWS_SKIP_REASON)]), windows=False, output='synthetic')

    def test_unexpected_missing_or_wrong_skip_is_rejected(self):
        good = [(WINDOWS_CASE_ID, WINDOWS_SKIP_REASON)]
        for windows, skips in [(True, good), (False, []),
                (False, [(WINDOWS_CASE_ID, 'different reason')]),
                (False, [(EXPECTED_CASE_IDS[0], WINDOWS_SKIP_REASON)]),
                (False, good + good),
                (False, good + [('retained_cpu_resource_contracts.foreign', 'Windows only')])]:
            with self.subTest(windows=windows, skips=skips):
                with self.assertRaises(AssertionError):
                    require_cpu_result(self, EXPECTED_CASE_IDS, self.result(skips),
                                       windows=windows, output='synthetic')

    def test_empty_changed_duplicate_or_missing_identity_is_rejected(self):
        for identities in [(), EXPECTED_CASE_IDS[:-1],
                EXPECTED_CASE_IDS[:-1] + ('foreign.test',),
                EXPECTED_CASE_IDS[:-1] + (EXPECTED_CASE_IDS[0],)]:
            with self.subTest(identities=identities):
                with self.assertRaises(AssertionError):
                    require_cpu_result(self, identities, self.result(), windows=True, output='synthetic')

    def test_count_and_failure_propagation_are_not_waived(self):
        for result in [self.result(count=0), self.result(count=13), self.result(success=False)]:
            with self.assertRaises(AssertionError):
                require_cpu_result(self, EXPECTED_CASE_IDS, result, windows=True, output='synthetic')
        with self.assertRaises(AssertionError):
            require_cpu_result(self, EXPECTED_CASE_IDS,
                self.result([(WINDOWS_CASE_ID, WINDOWS_SKIP_REASON)], success=False),
                windows=False, output='synthetic')


if __name__ == '__main__':
    unittest.main()
