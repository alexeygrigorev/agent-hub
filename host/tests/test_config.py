from unittest.mock import patch
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import json
import os
import tempfile
import unittest
from pathlib import Path
from agent_hub_host import config

class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patcher = patch("agent_hub_host.config.get_config_path")
        self.mock_get_path = self.patcher.start()
        self.mock_get_path.return_value = Path(self.tmpdir.name) / "host.json"
        # Worker-profile guard (incident edb71968): save_config/load_config
        # prefer the worker profile resolved from APLEXER_TAG/
        # APLEXER_WORKSPACE (or HUB_WORKER_CONFIG_FILE), bypassing the
        # patched get_config_path. Without this pin, a suite run from an
        # aplexer session shell writes fixtures into that session's real
        # hub-worker-auth file and destroys its credential (same failure
        # as test_machine_binding before its pin).
        self.worker_path = patch("agent_hub_host.worker_auth.config_path", return_value=None)
        self.worker_path.start()
        self.addCleanup(self.worker_path.stop)
        # Intake 6b624e65 AC3: human-first load_config() must never resolve the
        # real (or canary) cli.json in tests; pin it to the same temp dir.
        human_patcher = patch("agent_hub_host.config.get_human_config_path")
        self.addCleanup(human_patcher.stop)
        human_patcher.start().return_value = Path(self.tmpdir.name) / "config" / "cli.json"


    def tearDown(self):
        self.patcher.stop()
        self.tmpdir.cleanup()

    @unittest.skipIf(sys.platform == 'win32', 'POSIX modes')
    def test_unsafe_profile_permissions_are_actionable_and_never_loaded(self):
        config.save_config({'access_token': 'private-fixture'})
        path = self.mock_get_path.return_value
        for directory_mode, file_mode in ((0o755, 0o600), (0o700, 0o644)):
            os.chmod(path.parent, directory_mode)
            os.chmod(path, file_mode)
            with self.assertRaises(config.ConfigPermissionError) as caught:
                config._load_from(path)
            self.assertIn('700', str(caught.exception))
            self.assertIn('600', str(caught.exception))
            self.assertNotIn('private-fixture', str(caught.exception))
        os.chmod(path, 0o600)
        self.assertEqual(config._load_from(path)['access_token'], 'private-fixture')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX symlinks')
    def test_symlink_profile_and_ancestors_refuse_loading_and_surface_permission_error(self):
        root = Path(self.tmpdir.name)
        real_dir = root / "real_dir"
        real_dir.mkdir(mode=0o700)
        real_file = real_dir / "cli.json"
        real_file.write_text('{"access_token": "fixture-token", "client_kind": "human"}')
        real_file.chmod(0o600)

        # 1. Profile file is a symlink to a valid profile
        file_link = real_dir / "link.json"
        file_link.symlink_to(real_file)
        with self.assertRaises(config.ConfigPermissionError) as caught:
            config._load_from(file_link)
        self.assertIn("Unsafe Hub profile", str(caught.exception))
        self.assertIn("Symlinks and multiply linked files are refused", str(caught.exception))

        # 2. Profile file is a dangling symlink
        dangling_file = real_dir / "dangling.json"
        dangling_file.symlink_to(real_dir / "nonexistent.json")
        with self.assertRaises(config.ConfigPermissionError) as caught:
            config._load_from(dangling_file)
        self.assertIn("Unsafe Hub profile", str(caught.exception))

        # 3. Parent directory is a symlink to a valid directory
        link_dir = root / "link_dir"
        link_dir.symlink_to(real_dir)
        with self.assertRaises(config.ConfigPermissionError) as caught:
            config._load_from(link_dir / "cli.json")
        self.assertIn("Unsafe Hub profile", str(caught.exception))

        # 4. Parent directory is a dangling symlink
        dangling_dir = root / "dangling_dir"
        dangling_dir.symlink_to(root / "nonexistent_dir")
        with self.assertRaises(config.ConfigPermissionError) as caught:
            config._load_from(dangling_dir / "cli.json")
        self.assertIn("Unsafe Hub profile", str(caught.exception))

        # 5. Grandparent directory is a symlink
        grandchild = link_dir / "sub"
        grandchild.mkdir(mode=0o700)
        (grandchild / "cli.json").write_text('{"access_token": "fixture-token"}')
        (grandchild / "cli.json").chmod(0o600)
        with self.assertRaises(config.ConfigPermissionError) as caught:
            config._load_from(grandchild / "cli.json")
        self.assertIn("Unsafe Hub profile", str(caught.exception))

        # 6. Grandparent directory is a dangling symlink
        with self.assertRaises(config.ConfigPermissionError) as caught:
            config._load_from(dangling_dir / "sub" / "cli.json")
        self.assertIn("Unsafe Hub profile", str(caught.exception))

        # 7. Non-existent file in a normal directory does not raise ConfigPermissionError
        self.assertEqual(config._load_from(real_dir / "absent.json"), {})

    def test_save_and_load(self):
        data = {"access_token": "foo"}
        config.save_config(data)
        
        loaded = config.load_config()
        self.assertEqual(loaded, data)
        
    def test_update_config(self):
        config.save_config({"access_token": "foo"})
        config.update_config(refresh_token="bar")
        
        loaded = config.load_config()
        self.assertEqual(loaded, {"access_token": "foo", "refresh_token": "bar"})

    def test_clear_config(self):
        config.save_config({"access_token": "foo"})
        config.clear_config()
        loaded = config.load_config()
        self.assertEqual(loaded, {})

    def test_acl_backend_failure_never_publishes_config(self):
        with patch.object(config.private_fs, 'atomic_write', side_effect=PermissionError('ACL unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Config save failed'):
                config.save_config({'access_token': 'fixture-only'})
        self.assertFalse(self.mock_get_path.return_value.exists())

    def test_reject_invalid_machine_id_and_origin(self):
        for bad_mid in ("", "   ", " mach-1 ", 123, False, ["m1"], "mach\n1"):
            with self.assertRaises(ValueError):
                config.save_config({"access_token": "tok", "machine_id": bad_mid})
        for bad_url in ("", "   ", "http://evil.example.com", "ftp://hub.example.com", "https://user:pass@hub.example.com"):
            with self.assertRaises(ValueError):
                config.save_config({"access_token": "tok", "base_url": bad_url})

        # Tampered file on disk fails closed to {}
        p = config.get_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"access_token": "tok", "base_url": "https://hub.example.com", "machine_id": "   "}))
        os.chmod(p, 0o600)
        self.assertEqual(config.load_config(), {})

    def test_register_machine_persists_validated_machine_id_same_origin(self):
        from agent_hub_host import api
        config.save_config({"base_url": "https://hub.example.com", "access_token": "tok123"})

        with patch("agent_hub_host.api.safe_http_json", return_value={"status": "registered", "machine_id": "mach-reg-01"}), \
             patch("agent_hub_host.system.get_system_info", return_value={"hostname": "h1"}):
            api.register_machine("https://hub.example.com")

        loaded = config.load_config()
        self.assertEqual(loaded.get("machine_id"), "mach-reg-01")
        self.assertEqual(loaded.get("base_url"), "https://hub.example.com")

        # Conflicting machine_id from server must be rejected
        with patch("agent_hub_host.api.safe_http_json", return_value={"status": "registered", "machine_id": "other-mach"}), \
             patch("agent_hub_host.system.get_system_info", return_value={"hostname": "h1"}):
            with self.assertRaises(RuntimeError):
                api.register_machine("https://hub.example.com")

        # Wrong origin must be rejected before HTTP call
        with patch("agent_hub_host.api.safe_http_json") as mock_http:
            with self.assertRaises(RuntimeError):
                api.register_machine("https://attacker.example.com")
            mock_http.assert_not_called()

    def test_device_login_persists_server_machine_id_via_safe_http_json(self):
        from agent_hub_host import auth
        from unittest.mock import MagicMock

        code_resp = MagicMock()
        code_resp.headers = {"Content-Type": "application/json"}
        code_resp.read.return_value = json.dumps({
            "device_code": "dc1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://hub.example.com/device",
            "interval": 0,
            "expires_in": 60,
        }).encode("utf-8")
        code_resp.__enter__.return_value = code_resp

        tok_resp = MagicMock()
        tok_resp.headers = {"Content-Type": "application/json"}
        tok_resp.read.return_value = json.dumps({
            "access_token": "hubt_tok1",
            "refresh_token": "hubr_ref1",
            "machine_id": "mach-paired-99",
        }).encode("utf-8")
        tok_resp.__enter__.return_value = tok_resp

        with patch("urllib.request.OpenerDirector.open", side_effect=[code_resp, tok_resp]):
            auth.device_login("https://hub.example.com", "host", "host-1", "myhost", ["host"])

        loaded = config.load_config()
        self.assertEqual(loaded.get("access_token"), "hubt_tok1")
        self.assertEqual(loaded.get("refresh_token"), "hubr_ref1")
        self.assertEqual(loaded.get("base_url"), "https://hub.example.com")
        self.assertEqual(loaded.get("machine_id"), "mach-paired-99")


