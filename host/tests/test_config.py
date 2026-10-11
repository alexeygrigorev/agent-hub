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
        self.patcher = unittest.mock.patch("agent_hub_host.config.get_config_path")
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
