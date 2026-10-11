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

    @patch("sys.platform", "win32")
    @patch("subprocess.run")
    def test_windows_acl_failure(self, mock_run):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "access denied"
        mock_run.return_value.stdout = r"domain\user,S-1-5-21-1234"
        with self.assertRaisesRegex(RuntimeError, "Failed to"):
            config._set_permissions(self.patcher.start().return_value)

