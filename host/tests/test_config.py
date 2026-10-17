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

    def tearDown(self):
        self.patcher.stop()
        self.tmpdir.cleanup()

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


