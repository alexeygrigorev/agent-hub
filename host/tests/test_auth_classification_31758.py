"""Native prerequisite failure is not evidence of an expired device login."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_hub_host import opencode_auth as auth
from agent_hub_host.fleet import Dispatcher


class DeviceAuthClassificationTests(unittest.TestCase):
    def config(self, root):
        return dict(binary='/installed/opencode', source_data=str(root / 'source'),
                    source_config=str(root / 'config'), target_data=str(root / 'runtime'))

    def test_export_process_failure_is_unavailable_not_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(Path(tmp))
            Path(cfg['target_data']).mkdir()
            proc = MagicMock(returncode=1)
            with patch.object(auth.subprocess, 'Popen', return_value=proc):
                with self.assertRaises(auth.AuthCheckUnavailable) as caught:
                    auth._native(cfg, ['auth', 'export', 'opencode'], {})
            self.assertNotIn('expired', str(caught.exception))

    def test_timeout_never_becomes_expired_and_never_exposes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(Path(tmp))
            with patch.object(auth, '_verified_recently', return_value=False), \
                 patch.object(auth, '_verification_identity', return_value=None):
                def native(*args):
                    raise subprocess.TimeoutExpired(['fixture-secret'], 45, output=b'fixture-secret')
                with self.assertRaises(auth.AuthCheckUnavailable) as caught:
                    auth.prepare(cfg, native=native)
            self.assertNotIn('fixture-secret', str(caught.exception))
            self.assertNotIn('expired', str(caught.exception))

    def test_missing_device_login_still_fails_closed_as_auth_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(auth, '_verified_recently', return_value=False), \
                 patch.object(auth, '_verification_identity', return_value=None):
                with self.assertRaisesRegex(ValueError, '^engine_auth_expired:'):
                    auth.prepare(self.config(Path(tmp)), native=lambda *args: [])

    def test_unavailable_verification_preserves_offer_without_auth_health_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = MagicMock()
            dispatcher = Dispatcher(dict(version=1, workspaces={'demo': tmp},
                                         subscriptions=[]), request, MagicMock())
            results = []
            with patch.object(dispatcher, 'dispatch', side_effect=auth.AuthCheckUnavailable('fixture-secret')):
                dispatcher._dispatch_one({'id': 'offered-probe'}, results)
            self.assertEqual(results, [{'id': 'offered-probe', 'state': 'held',
                                        'reason': 'engine_auth_check_unavailable'}])
            request.assert_not_called()
