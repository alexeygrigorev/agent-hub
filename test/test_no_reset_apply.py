"""Alexey t1741u: Hub may track expiry and ask for approval, never redeem a reset."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NoResetApplyTests(unittest.TestCase):
    def test_no_provider_reset_redemption_endpoint_in_hub_runtime(self):
        forbidden = re.compile(
            r'rate-limit-reset-credits/(?:consume|redeem)'
            r'|account/rateLimitResetCredit/consume'
            r'|customer-package-reset/use', re.I)
        matches = []
        for folder, suffixes in [('src', {'.ts'}), ('host/agent_hub_host', {'.py'}), ('public', {'.js'})]:
            for path in (ROOT / folder).rglob('*'):
                if path.suffix in suffixes and 'vendor' not in path.parts and forbidden.search(path.read_text()):
                    matches.append(str(path.relative_to(ROOT)))
        self.assertEqual(matches, [], 'Provider reset redemption must never be implemented in Hub')

    def test_alert_service_has_no_provider_transport_or_execution_dependency(self):
        source = (ROOT / 'src/banked-resets.ts').read_text()
        imports = re.findall(r"from ['\"]([^'\"]+)['\"]", source)
        self.assertEqual(imports, ['@cloudflare/workers-types', './liveness'])
        self.assertIsNone(re.search(r'\b(?:fetch|spawn|execFile|subprocess|redeem|consume)\s*\(', source))
        self.assertIn('Alexey, please approve', source)
        self.assertIn('Hub and agents must not redeem it', source)


if __name__ == '__main__':
    unittest.main()
