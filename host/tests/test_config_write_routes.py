"""Human credentials survive host writes and test-only deletion attempts."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import auth, config


class HumanWriteRouteProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / 'config' / 'cli.json'
        self.profile.parent.mkdir(mode=0o700)
        override = patch.dict(os.environ, {'HUB_CONFIG_FILE': str(self.profile)})
        override.start()
        self.addCleanup(override.stop)
        config.save_human_config({'client_kind': 'human', 'access_token': 'fixture-human',
                                 'refresh_token': 'fixture-refresh', 'base_url': 'https://hub.example'})

    def test_host_write_routes_preserve_human(self):
        before = self.profile.read_bytes()
        foreign = {'client_kind': 'host', 'access_token': 'fixture-host'}
        writers = (
            lambda: config._save_to(self.profile, foreign),
            lambda: config.save_config(foreign),
            lambda: config.save_host_config(foreign),
            lambda: config.update_config(access_token='fixture-host'),
        )
        for index, writer in enumerate(writers):
            with self.subTest(route=index):
                with self.assertRaises(config.ConfigPermissionError):
                    writer()
                self.assertEqual(self.profile.read_bytes(), before)

    def test_single_file_human_refresh_still_rotates(self):
        def response(request, timeout=10.0, _token_exchange=None):
            result = {'access_token': 'fixture-rotated', 'refresh_token': 'fixture-refreshed'}
            _token_exchange.record(result)
            return result
        with patch('agent_hub_host.api.safe_http_json', side_effect=response):
            self.assertEqual(auth.refresh_token(), 'fixture-rotated')
        self.assertEqual(config.load_human_config()['refresh_token'], 'fixture-refreshed')

    def test_deletion_honors_guard_for_direct_and_symlink_paths(self):
        real = self.root / 'real-home'
        path = real / '.config' / 'agent-hub' / 'cli.json'
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_text('canary-only')
        link = self.root / 'linked-config'
        link.symlink_to(real / '.config', target_is_directory=True)
        for requested in (path, link / 'agent-hub' / 'cli.json'):
            for clearer in (config.clear_config, config.clear_human_config):
                with self.subTest(path=requested, clearer=clearer.__name__):
                    with patch.dict(os.environ, {config.TEST_GUARD_HOME_ENV: str(real)}), \
                         patch.object(config, 'get_config_path', return_value=requested), \
                         patch.object(config, 'get_human_config_path', return_value=requested):
                        with self.assertRaises(config.ConfigPermissionError):
                            clearer()
                    self.assertEqual(path.read_text(), 'canary-only')


if __name__ == '__main__':
    unittest.main()
