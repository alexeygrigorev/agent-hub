import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import json
import unittest
from unittest.mock import patch, MagicMock, ANY
import urllib.error
from agent_hub_host import auth, config

class TestAuth(unittest.TestCase):
    @patch("urllib.request.OpenerDirector.open")
    @patch("agent_hub_host.config.save_config")
    @patch("agent_hub_host.config.load_config")
    def test_device_flow_success(self, mock_load_config, mock_update_config, mock_urlopen):
        mock_load_config.return_value = {}
        
        # Mock /code
        code_resp = MagicMock()
        code_resp.headers = {"Content-Type": "application/json"}
        code_resp.read.return_value = json.dumps({
            "device_code": "dev123",
            "user_code": "usr123",
            "verification_uri": "https://fake/verify",
            "interval": 0.1,
            "expires_in": 10
        }).encode("utf-8")
        code_resp.__enter__.return_value = code_resp
        
        # Mock /token pending then success
        pending_resp = MagicMock()
        pending_resp.read.return_value = json.dumps({"error": "authorization_pending"}).encode("utf-8")
        pending_error = urllib.error.HTTPError("url", 400, "Bad Request", {}, pending_resp)
        
        success_resp = MagicMock()
        success_resp.headers = {"Content-Type": "application/json"}
        success_resp.read.return_value = json.dumps({
            "access_token": "acc123",
            "refresh_token": "ref123"
        }).encode("utf-8")
        success_resp.__enter__.return_value = success_resp
        
        mock_urlopen.side_effect = [code_resp, pending_error, success_resp]
        
        auth.device_login("https://fake", "host", "agent-hub-host", "host", auth.HOST_SCOPES)
        mock_update_config.assert_called_once_with(dict(access_token="acc123", refresh_token="ref123", base_url="https://fake", client_kind="host", client_name="agent-hub-host", requested_scopes=auth.HOST_SCOPES))

    @patch("urllib.request.OpenerDirector.open")
    @patch("agent_hub_host.config.save_config")
    @patch("agent_hub_host.config.load_config", return_value={})
    def test_device_flow_slowdown_and_deny(self, mock_load_config, mock_update_config, mock_urlopen):
        code_resp = MagicMock()
        code_resp.headers = {"Content-Type": "application/json"}
        code_resp.read.return_value = json.dumps({
            "device_code": "dev123",
            "user_code": "usr123",
            "verification_uri": "https://fake/verify",
            "interval": 0.1,
            "expires_in": 10
        }).encode("utf-8")
        code_resp.__enter__.return_value = code_resp
        
        slow_resp = MagicMock()
        slow_resp.read.return_value = json.dumps({"error": "slow_down"}).encode("utf-8")
        slow_error = urllib.error.HTTPError("url", 400, "Bad Request", {}, slow_resp)
        
        deny_resp = MagicMock()
        deny_resp.read.return_value = json.dumps({"error": "access_denied"}).encode("utf-8")
        deny_error = urllib.error.HTTPError("url", 400, "Bad Request", {}, deny_resp)
        
        mock_urlopen.side_effect = [code_resp, slow_error, deny_error]
        
        with self.assertRaisesRegex(RuntimeError, "Access denied by user."):
            auth.device_login("https://fake", "host", "agent-hub-host", "host", auth.HOST_SCOPES)
        mock_update_config.assert_not_called()

    @patch("urllib.request.OpenerDirector.open")
    @patch("agent_hub_host.config.load_config")
    @patch("agent_hub_host.config.save_config")
    def test_refresh_token_success(self, mock_update_config, mock_load_config, mock_urlopen):
        mock_load_config.return_value = {"refresh_token": "ref123", "base_url": "https://fake"}
        
        success_resp = MagicMock()
        success_resp.headers = {"Content-Type": "application/json"}
        success_resp.read.return_value = json.dumps({
            "access_token": "new_acc",
            "refresh_token": "new_ref"
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = success_resp
        
        token = auth.refresh_token()
        self.assertEqual(token, "new_acc")
        mock_update_config.assert_called_once_with(dict(base_url="https://fake", access_token="new_acc", refresh_token="new_ref"))

    @patch("urllib.request.OpenerDirector.open")
    @patch("agent_hub_host.config.load_config")
    @patch("agent_hub_host.auth.device_login")
    @patch("agent_hub_host.auth.get_access_token")
    @patch("agent_hub_host.config.clear_config")
    def test_refresh_token_revoked(self, mock_clear_config, mock_get_access_token, mock_device_login, mock_load_config, mock_urlopen):
        mock_load_config.return_value = {"refresh_token": "ref123", "base_url": "https://fake"}
        mock_get_access_token.return_value = "acc999"
        
        deny_resp = MagicMock()
        deny_resp.read.return_value = json.dumps({"error": "invalid_grant"}).encode("utf-8")
        deny_error = urllib.error.HTTPError("url", 400, "Bad Request", {}, deny_resp)
        mock_urlopen.side_effect = deny_error
        
        res = auth.refresh_token()
        self.assertEqual(res, "acc999")
        # Keep the old private config until a successful replacement is saved.
        mock_clear_config.assert_not_called()
        mock_device_login.assert_called_once()
