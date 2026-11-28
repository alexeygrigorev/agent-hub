"""Alexey t1741u: Hub may track expiry and ask for approval, never redeem a reset."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NoResetApplyTests(unittest.TestCase):
    def test_no_provider_reset_redemption_endpoint_in_hub_runtime(self):
        forbidden = re.compile(
            r'rate-limit-reset-credits/(?:consume|redeem)'
            r'|account/rateLimitResetCredit/consume'
            r'|customer-package-reset/use', re.I)
        matches = []
        for folder, suffixes in [('src', {'.ts'}), ('host/agent_hub_host', {'.py'}), ('public', {'.js'}),
                                 ('hub2/src', {'.ts'}), ('hub2/host/agent_hub_host', {'.py'}),
                                 ('hub2/public', {'.js', '.html'})]:
            for path in (ROOT / folder).rglob('*'):
                if path.suffix in suffixes and 'vendor' not in path.parts and forbidden.search(path.read_text()):
                    matches.append(str(path.relative_to(ROOT)))
        self.assertEqual(matches, [], 'Provider reset redemption must never be implemented in Hub')

    def test_alert_service_has_no_provider_transport_or_execution_dependency(self):
        # BankedResets was a Hub1-only service. The maintained AccountDO owns
        # reset observations/expiry alerts, not provider redemption or execution.
        source = (ROOT / 'hub2/src/account-do.ts').read_text()
        resets = source.split(' private async resets(', 1)[1].split(' private async credentials(', 1)[0]
        alerts = source.split(' private resetAlerts():', 1)[1].split(' private alerts(', 1)[0]
        for leaf in (resets, alerts):
            self.assertIsNone(re.search(
                r'\b(?:fetch|spawn|execFile|subprocess|redeem|consume|provider)\s*\(', leaf))
        self.assertIn("if(r.method!=='GET')return error('not_found',404)", resets)
        self.assertIn("redemption:'explicit_approval_required'", resets)
        self.assertIn("'reset_expiry_'+stage", alerts)
        # Runtime fake-provider/unauthorized-operation checks live in the required
        # accounts-mail Node module; this leaf check must not ban quota discovery.


if __name__ == '__main__':
    unittest.main()
