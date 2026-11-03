import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import monitoring_common


class MonitoringCommonTests(unittest.TestCase):
    def test_explicit_config_path_precedes_environment(self):
        with patch.dict(os.environ, {"HUB_TEST_MONITOR_CONFIG": "from-env"}):
            self.assertEqual(
                monitoring_common.resolve_config_path(
                    "explicit", env_var="HUB_TEST_MONITOR_CONFIG", filename="monitor.json"
                ),
                "explicit",
            )

    def test_environment_config_path_precedes_cwd_candidates(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"HUB_TEST_MONITOR_CONFIG": "from-env"}
        ), patch("agent_hub_host.monitoring_common.os.getcwd", return_value=root):
            self.assertEqual(
                monitoring_common.resolve_config_path(
                    None, env_var="HUB_TEST_MONITOR_CONFIG", filename="monitor.json"
                ),
                "from-env",
            )

    def test_existing_local_config_precedes_journal_and_missing_path_is_local(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {}, clear=True
        ), patch("agent_hub_host.monitoring_common.os.getcwd", return_value=root):
            local = Path(root) / ".local" / "session" / "monitor.json"
            journal = Path(root) / "journal" / "monitor.json"
            local.parent.mkdir(parents=True)
            journal.parent.mkdir(parents=True)
            local.touch()
            journal.touch()
            self.assertEqual(
                monitoring_common.resolve_config_path(
                    None, env_var="HUB_TEST_MONITOR_CONFIG", filename="monitor.json"
                ),
                str(local),
            )
            local.unlink()
            self.assertEqual(
                monitoring_common.resolve_config_path(
                    None, env_var="HUB_TEST_MONITOR_CONFIG", filename="monitor.json"
                ),
                str(journal),
            )
            journal.unlink()
            self.assertEqual(
                monitoring_common.resolve_config_path(
                    None, env_var="HUB_TEST_MONITOR_CONFIG", filename="monitor.json"
                ),
                str(local),
            )

    def test_utc_timestamp_keeps_millisecond_z_format(self):
        self.assertRegex(
            monitoring_common.utc_now_iso(),
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        )

    def test_origin_and_token_precedence_and_url_validation(self):
        with patch.dict(
            os.environ,
            {"HUB_URL": "https://env.example/", "HUB_TOKEN": "env-token"},
            clear=True,
        ), patch("agent_hub_host.config.load_config", return_value={"base_url": "https://config.example"}), patch(
            "agent_hub_host.api.check_url"
        ) as check_url:
            self.assertEqual(
                monitoring_common.resolve_origin_and_token(
                    "https://explicit.example/", "explicit-token", missing_token_message="missing"
                ),
                ("https://explicit.example", "explicit-token"),
            )
            check_url.assert_called_once_with("https://explicit.example/")

    def test_origin_uses_config_and_auth_fallback_and_preserves_error(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "agent_hub_host.config.load_config", return_value={"base_url": "https://config.example/"}
        ), patch("agent_hub_host.api.check_url") as check_url, patch(
            "agent_hub_host.auth.get_access_token", return_value="stored-token"
        ):
            self.assertEqual(
                monitoring_common.resolve_origin_and_token(
                    None, None, missing_token_message="missing"
                ),
                ("https://config.example", "stored-token"),
            )
            check_url.assert_called_once_with("https://config.example/")

        with patch.dict(os.environ, {}, clear=True), patch(
            "agent_hub_host.config.load_config", return_value={}
        ), patch("agent_hub_host.api.check_url"), patch(
            "agent_hub_host.auth.get_access_token", return_value=None
        ), self.assertRaisesRegex(ValueError, "disk monitoring token missing"):
            monitoring_common.resolve_origin_and_token(
                None, None, missing_token_message="disk monitoring token missing"
            )


if __name__ == "__main__":
    unittest.main()
