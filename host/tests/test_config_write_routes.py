"""Human credentials survive host writes and test-only deletion attempts."""
import os
import sys
import tempfile
import threading
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

    def test_host_save_cannot_race_human_login_and_replace_the_result(self):
        self.profile.unlink()
        host_entered_write = threading.Event()
        release_host_write = threading.Event()
        human_started = threading.Event()
        human_entered_write = threading.Event()
        errors = []
        original = config.private_fs.atomic_write

        def coordinated_write(path, raw, limit, **kwargs):
            payload = raw.decode('utf-8')
            if 'fixture-host' in payload:
                host_entered_write.set()
                if not release_host_write.wait(5):
                    raise AssertionError('host writer was not released')
            if 'fixture-human-after-race' in payload:
                human_entered_write.set()
            return original(path, raw, limit, **kwargs)

        def host_writer():
            try:
                config.save_config({'client_kind': 'host', 'access_token': 'fixture-host'})
            except Exception as exc:
                errors.append(exc)

        def human_writer():
            try:
                human_started.set()
                config.save_human_config({
                    'client_kind': 'human', 'access_token': 'fixture-human-after-race',
                    'refresh_token': 'fixture-refresh-after-race', 'base_url': 'https://hub.example',
                })
            except Exception as exc:
                errors.append(exc)

        with patch.object(config.private_fs, 'atomic_write', side_effect=coordinated_write):
            host = threading.Thread(target=host_writer)
            host.start()
            self.assertTrue(host_entered_write.wait(5), 'host writer did not reach its save')
            human = threading.Thread(target=human_writer)
            human.start()
            self.assertTrue(human_started.wait(5))
            self.assertFalse(human_entered_write.wait(.1), 'human and host writes were not serialized')
            release_host_write.set()
            host.join(5)
            human.join(5)

        self.assertFalse(host.is_alive())
        self.assertFalse(human.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(human_entered_write.is_set())
        saved = config.load_human_config()
        self.assertEqual(saved.get('access_token'), 'fixture-human-after-race')
        self.assertEqual(saved.get('refresh_token'), 'fixture-refresh-after-race')

    def test_clear_config_cannot_remove_human_profile(self):
        before = self.profile.read_bytes()
        with self.assertRaises(config.ConfigPermissionError):
            config.clear_config()
        self.assertEqual(self.profile.read_bytes(), before)

    def test_host_writer_cannot_create_config_at_default_human_path(self):
        self.profile.unlink()
        with patch.object(config, '_default_human_config_path', return_value=self.profile):
            with self.assertRaises(config.ConfigPermissionError):
                config.save_config({'client_kind': 'host', 'access_token': 'fixture-host'})
            with self.assertRaises(config.ConfigPermissionError):
                config.clear_config()
        self.assertFalse(self.profile.exists())

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
