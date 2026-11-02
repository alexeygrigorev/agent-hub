import unittest

from agent_hub_host.launch_diagnostics import REFUSALS, diagnostic


class MemoryRefusalNumbersTests(unittest.TestCase):
    """Intake 63a9ee3a: the launcher refusal carries its headroom numbers."""

    TAIL = ('host MemAvailable below 2048MiB floor after reservation (launcher_memory_reserve): '
            'available_mb=4700 reserved_mb=2925 required_mb=1500 floor_mb=2048')

    def test_numeric_refusal_is_classified_and_surfaced(self):
        info = diagnostic('Traceback (most recent call last):\nValueError: ' + self.TAIL)
        self.assertEqual((info['reason'], info['message']), REFUSALS['launcher_memory_reserve'])
        self.assertEqual(info['tail'], 'ValueError: ' + self.TAIL)

    def test_legacy_refusal_still_matches(self):
        legacy = 'host MemAvailable below 2048MiB floor after reservation (launcher_memory_reserve)'
        self.assertEqual(diagnostic('ValueError: ' + legacy)['reason'], 'launcher_memory_reserve')

    def test_non_numeric_suffix_is_withheld(self):
        for unsafe in (self.TAIL + ' secret=x', self.TAIL.replace('4700', '/private/token'),
                       self.TAIL.replace('4700', '9' * 40)):
            with self.subTest(unsafe=unsafe):
                info = diagnostic('ValueError: ' + unsafe)
                self.assertEqual(info['reason'], 'launcher_command_failed')
                self.assertNotIn('private', str(info))


if __name__ == '__main__':
    unittest.main()
