import unittest
import sys
import os
import json
import io
import stat
import tempfile
import importlib.util
from unittest.mock import patch, MagicMock
import urllib.error

script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts', 'import-team-registry.py'))
spec = importlib.util.spec_from_file_location("import_team_registry", script_path)
itr = importlib.util.module_from_spec(spec)
sys.modules["import_team_registry"] = itr
spec.loader.exec_module(itr)

class TestRegistryImport(unittest.TestCase):
    def test_flatten_teams_list(self):
        data = {
            "teams": [
                {
                    "id": "team1",
                    "project": "agent-hub",
                    "host": "h1",
                    "agents": [
                        {
                            "tag": "head1",
                            "role": "head",
                            "workspace": "/tmp",
                            "provider": "codex",
                            "model": "gpt-5",
                            "status": "active"
                        }
                    ]
                }
            ],
            "agents": [
                {
                    "tag": "w1",
                    "role": "writer",
                    "team_id": "team2",
                    "workspace": "/w",
                    "engine": "anthropic"
                }
            ]
        }
        res = itr.flatten_registry(data)
        tags = {r["tag"]: r for r in res}
        self.assertIn("head1", tags)
        self.assertIn("w1", tags)
        self.assertEqual(tags["head1"]["role"], "head")
        self.assertEqual(tags["head1"]["engine"], "codex")
        self.assertEqual(tags["head1"]["machine_id"], "h1")
        # Ensure stale health/status is NOT in the imported output
        self.assertNotIn("status", tags["head1"])
        self.assertNotIn("state", tags["head1"])

    def test_unknown_fields_rejected(self):
        data = {
            "teams": [{"id": "team1", "agents": [{"tag": "head1", "role": "head", "invented_bogus_field": "123"}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(data)
        self.assertIn("Unknown field", str(ctx.exception))

    def test_sensitive_fields_rejected(self):
        sensitive_cases = ["token", "secret", "password", "api_key", "credential"]
        for field in sensitive_cases:
            data = {
                "teams": [{"id": "team1", "agents": [{"tag": "a1", "role": "worker", field: "super_secret"}]}]
            }
            with self.assertRaises(ValueError) as ctx:
                itr.flatten_registry(data)
            self.assertIn("Sensitive field rejected", str(ctx.exception))

    def test_unknown_roles_rejected(self):
        data = {
            "teams": [{"id": "team1", "agents": [{"tag": "a1", "role": "super_arbitrary_unmappable_role_xyz"}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(data)
        self.assertIn("Unknown role", str(ctx.exception))

    def test_duplicate_tag_ambiguity_conflicting_project(self):
        data = {
            "teams": [
                {
                    "id": "team1",
                    "project": "proj-a",
                    "agents": [{"tag": "worker-dup", "role": "worker"}]
                }
            ],
            "agents": [
                {"tag": "worker-dup", "project_id": "proj-b", "role": "worker"}
            ]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(data)
        self.assertIn("Duplicate tag ambiguity", str(ctx.exception))

    def test_url_safety(self):
        # Valid URLs
        self.assertTrue(itr.is_safe_url("https://hub.dtcdev.click/api/agents/import"))
        self.assertTrue(itr.is_safe_url("http://localhost:8787/api/agents/import"))
        self.assertTrue(itr.is_safe_url("http://127.0.0.1:8787/api/agents/import"))
        self.assertTrue(itr.is_safe_url("http://[::1]:8787/api/agents/import"))

        # Insecure or malicious URLs
        self.assertFalse(itr.is_safe_url("http://remote-server.com/api/agents/import"))
        self.assertFalse(itr.is_safe_url("ftp://example.com/api/agents/import"))
        self.assertFalse(itr.is_safe_url("file:///etc/passwd"))
        # Userinfo (credentials) forbidden
        self.assertFalse(itr.is_safe_url("https://admin:secret@hub.dtcdev.click/api/agents/import"))
        self.assertFalse(itr.is_safe_url("http://admin@localhost:8787/api/agents/import"))
        # Invalid port
        self.assertFalse(itr.is_safe_url("http://localhost:70000/api/agents/import"))
        # Control characters
        self.assertFalse(itr.is_safe_url("https://hub.dtcdev.click/\r\n/api/agents/import"))

    def test_importer_unbounded_response_read(self):
        """Verifies bounded response read prevents memory exhaustion on oversized response."""
        mock_resp = MagicMock()
        # Returns > 10MB to trigger size guard
        mock_resp.read.return_value = b"x" * (10 * 1024 * 1024 + 1)
        mock_resp.status = 200

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            with patch("sys.exit") as mock_exit:
                itr.do_import([{"tag": "test-agent"}], "https://hub.dtcdev.click/api/agents/import", "fake-token")
                mock_exit.assert_called_with(1)

    def test_importer_cli_missing_url_scheme_validation(self):
        """Verifies CLI halts on unsafe URL without scheme or with forbidden scheme."""
        with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            with patch("sys.exit") as mock_exit:
                itr.do_import([{"tag": "test-agent"}], "ftp://unsafe-hub.com", "fake-token")
                mock_exit.assert_called_with(1)
            self.assertIn("Unsafe URL", mock_stderr.getvalue())

    def test_importer_http_error_bounded_read(self):
        """Verifies HTTPError body read is bounded and error is sanitized."""
        err = urllib.error.HTTPError("https://hub.dtcdev.click", 500, "Internal Server Error", {}, io.BytesIO(b"error details"))
        with patch("urllib.request.urlopen", side_effect=err):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                with patch("sys.exit") as mock_exit:
                    itr.do_import([{"tag": "test-agent"}], "https://hub.dtcdev.click/api/agents/import", "fake-token")
                    mock_exit.assert_called_with(1)
                self.assertIn("Server error: 500", mock_stderr.getvalue())

    def test_token_file_permissions(self):
        """Verifies import token file requires mode 600."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("test-token-12345\n")
            token_path = f.name
        try:
            # Set mode 644 (world readable)
            os.chmod(token_path, 0o644)
            with patch("os.path.expanduser", return_value=token_path):
                with patch.dict(os.environ, {}, clear=True):
                    with patch("sys.exit") as mock_exit:
                        itr.get_token()
                        mock_exit.assert_called_with(1)

            # Set mode 600 (owner only)
            os.chmod(token_path, 0o600)
            with patch("os.path.expanduser", return_value=token_path):
                with patch.dict(os.environ, {}, clear=True):
                    tok = itr.get_token()
                    self.assertEqual(tok, "test-token-12345")
        finally:
            if os.path.exists(token_path):
                os.remove(token_path)

    def test_representative_registry_parsing_without_leaking(self):
        """Exercise the real parser on a fixed large registry without host-file dependencies."""
        data = {
            "teams": [
                {
                    "id": f"team-{i}",
                    "project": "agent-hub",
                    "host": "h1",
                    "agents": [
                        {
                            "tag": f"agent-{i}-{j}",
                            "role": "worker" if j > 0 else "head",
                            "workspace": f"/tmp/w{i}_{j}",
                            "session_id": f"s-{i}-{j}",
                        }
                        for j in range(10)
                    ],
                }
                for i in range(21)
            ]
        }

        agents = itr.flatten_registry(data)
        self.assertGreater(len(agents), 200)
        # Verify all mapped agents have valid canonical roles
        for a in agents:
            self.assertIn(a["role"], itr.CANONICAL_ROLES)
            self.assertTrue(bool(a["tag"]))

    def test_real_format_regression_session_history(self):
        """Regression test for TEAM-REGISTRY format with session_history without leaking historical data."""
        data = {
            "teams": [
                {
                    "id": "agent-hub",
                    "project": "agent-hub",
                    "host": "hetzner-host",
                    "agents": [
                        {
                            "tag": "agent-hub-head",
                            "role": "head",
                            "workspace": "/home/alexey/git/agent-hub",
                            "session_id": "active-head-session-123",
                            "session_history": [
                                {
                                    "session_id": "historical-session-old-456",
                                    "ended": "2026-10-11T12:00:00Z",
                                    "engine": "codex",
                                    "mode": "interactive",
                                    "observation": "previous head run finished",
                                    "recorded_at": "2026-10-11T12:00:01Z",
                                    "registered_at": "2026-10-11T10:00:00Z",
                                    "registered_by": "root",
                                    "launch": "aplexer"
                                }
                            ]
                        },
                        {
                            "tag": "unbound-worker",
                            "role": "implementer",
                            "workspace": "/home/alexey/git/agent-hub/ws",
                            "session_history": [
                                {
                                    "session_id": "historical-worker-session-789",
                                    "ended": "2026-10-10T15:00:00Z",
                                    "recorded_at": "2026-10-10T15:00:01Z"
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        agents = itr.flatten_registry(data)
        by_tag = {a["tag"]: a for a in agents}
        
        # Verify active session is preserved and historical session is not used
        self.assertEqual(by_tag["agent-hub-head"]["session_id"], "active-head-session-123")
        self.assertIsNone(by_tag["unbound-worker"]["session_id"])
        
        # Verify session_history and health fields are not exported
        for a in agents:
            self.assertNotIn("session_history", a)
            self.assertNotIn("status", a)
            self.assertNotIn("health", a)
            self.assertNotIn("state", a)

    def test_metadata_shape_validation(self):
        """Verifies shape validation for nested metadata and rejection of malformed structures."""
        # Non-list session_history
        bad_sh1 = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "session_history": "not-a-list"}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(bad_sh1)
        self.assertIn("Invalid session_history", str(ctx.exception))

        # Non-dict item in session_history
        bad_sh2 = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "session_history": ["not-a-dict"]}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(bad_sh2)
        self.assertIn("Invalid session_history item", str(ctx.exception))

        # Unknown field in session_history item
        bad_sh3 = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "session_history": [{"session_id": "s1", "raw_prompt": "hello"}]}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(bad_sh3)
        self.assertIn("Unknown field", str(ctx.exception))

        # Unknown field in first_action
        bad_fa = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "first_action": {"invented_first_action_key": 1}}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(bad_fa)
        self.assertIn("Unknown field", str(ctx.exception))

        # Unknown field in telemetry
        bad_tel = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "telemetry": {"invented_telemetry_key": 2}}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(bad_tel)
        self.assertIn("Unknown field", str(ctx.exception))

        # Unknown field in head_history item
        bad_hh = {
            "teams": [{"id": "t1", "head_history": [{"invented_hh_key": 3}], "agents": [{"tag": "a1", "role": "worker"}]}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(bad_hh)
        self.assertIn("Unknown field", str(ctx.exception))

    def test_nested_sensitive_values_never_exported(self):
        """Verifies rejection of sensitive keys inside nested structures and non-export of metadata."""
        sensitive_keys = ["secret", "token", "password", "credential"]
        for key in sensitive_keys:
            data = {
                "teams": [
                    {
                        "id": "t1",
                        "agents": [
                            {
                                "tag": "a1",
                                "role": "worker",
                                "session_history": [{"session_id": "s1", key: "leaked_val"}]
                            }
                        ]
                    }
                ]
            }
            with self.assertRaises(ValueError) as ctx:
                itr.flatten_registry(data)
            self.assertIn("Sensitive field rejected", str(ctx.exception))

        # Positive case: valid metadata fields are never exported in agent payload
        clean_data = {
            "teams": [
                {
                    "id": "t1",
                    "project": "proj-x",
                    "host": "host-x",
                    "head_history": [{"head_tag": "h1", "at": "2026-10-12"}],
                    "agents": [
                        {
                            "tag": "a1",
                            "role": "worker",
                            "workspace": "/w",
                            "session_id": "sess-1",
                            "engine": "codex",
                            "first_action": {"first_tool": "view_file", "first_tool_at_utc": "2026-10-12T00:00:00Z"},
                            "telemetry": {"conversation_id": "c1", "scope": "task"},
                            "session_history": [{"session_id": "s0", "ended": "2026-10-11T00:00:00Z"}]
                        }
                    ]
                }
            ]
        }
        agents = itr.flatten_registry(clean_data)
        self.assertEqual(len(agents), 1)
        a = agents[0]
        # Only canonical fields exported
        expected_keys = {"tag", "role", "team_id", "project", "machine_id", "workspace", "session_id", "engine", "imported_from"}
        self.assertEqual(set(a.keys()), expected_keys)
        self.assertEqual(a["tag"], "a1")
        self.assertEqual(a["session_id"], "sess-1")
        self.assertNotIn("first_action", a)
        self.assertNotIn("telemetry", a)
        self.assertNotIn("session_history", a)

    def test_contradictory_session_workspace_machine_duplicates(self):
        """Verifies duplicate conflict detection and deterministic ambiguity resolution."""
        # 1. Contradictory session
        data_sess = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "session_id": "s1"}]}],
            "agents": [{"tag": "a1", "role": "worker", "session_id": "s2"}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(data_sess)
        self.assertIn("Duplicate tag ambiguity", str(ctx.exception))

        # 2. Contradictory machine
        data_mach = {
            "teams": [{"id": "t1", "host": "m1", "agents": [{"tag": "a1", "role": "worker"}]}],
            "agents": [{"tag": "a1", "role": "worker", "machine_id": "m2"}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(data_mach)
        self.assertIn("Duplicate tag ambiguity", str(ctx.exception))

        # 3. Contradictory workspace (with same session)
        data_ws_sess = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "session_id": "s1", "workspace": "/w1"}]}],
            "agents": [{"tag": "a1", "role": "worker", "session_id": "s1", "workspace": "/w2"}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(data_ws_sess)
        self.assertIn("Duplicate tag ambiguity", str(ctx.exception))

        # 4. Contradictory workspace (both active, neither completed)
        data_ws_active = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "workspace": "/w1"}]}],
            "agents": [{"tag": "a1", "role": "worker", "workspace": "/w2"}]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(data_ws_active)
        self.assertIn("Duplicate tag ambiguity", str(ctx.exception))

        # 5. Deterministic adoption: bound session in agents updates unbound session in teams
        data_bound = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "workspace": "/w-default"}]}],
            "agents": [{"tag": "a1", "role": "worker", "session_id": "live-sess-1", "workspace": "/w-active", "machine_id": "host-live"}]
        }
        res_bound = itr.flatten_registry(data_bound)
        self.assertEqual(len(res_bound), 1)
        self.assertEqual(res_bound[0]["session_id"], "live-sess-1")
        self.assertEqual(res_bound[0]["workspace"], "/w-active")
        self.assertEqual(res_bound[0]["machine_id"], "host-live")

        # 6. Deterministic adoption: active workspace updates completed workspace
        data_completed = {
            "teams": [{"id": "t1", "agents": [{"tag": "a1", "role": "worker", "workspace": "/w-old", "completed_at": "2026-10-01"}]}],
            "agents": [{"tag": "a1", "role": "worker", "workspace": "/w-new"}]
        }
        res_comp = itr.flatten_registry(data_completed)
        self.assertEqual(len(res_comp), 1)
        self.assertEqual(res_comp[0]["workspace"], "/w-new")

    def test_unbound_identity_preserved_honestly(self):
        """Verifies unbound identities remain honest (None) without synthetic UUIDs or fake health."""
        data = {
            "agents": [
                {
                    "tag": "unbound-agent",
                    "role": "worker"
                }
            ]
        }
        res = itr.flatten_registry(data)
        self.assertEqual(len(res), 1)
        agent = res[0]
        self.assertIsNone(agent["session_id"])
        self.assertIsNone(agent["workspace"])
        self.assertIsNone(agent["machine_id"])
        self.assertNotIn("status", agent)
        self.assertNotIn("health", agent)
        self.assertNotIn("available", agent)
        self.assertNotIn("heartbeat", agent)

        # Malformed tag raises
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry({"agents": [{"role": "worker"}]})
        self.assertIn("Agent missing valid tag", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry({"agents": [{"tag": "", "role": "worker"}]})
        self.assertIn("Agent missing valid tag", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry({"agents": [{"tag": 12345, "role": "worker"}]})
        self.assertIn("Agent missing valid tag", str(ctx.exception))

    def test_import_count_and_caps(self):
        """Verifies agent count cap and payload size limits."""
        # > 1000 agents rejected
        oversized = {
            "agents": [{"tag": f"worker-{i}", "role": "worker"} for i in range(1001)]
        }
        with self.assertRaises(ValueError) as ctx:
            itr.flatten_registry(oversized)
        self.assertIn("Too many agents: exceeds 1000", str(ctx.exception))

        # Payload size cap > 1MB in do_import
        huge_payload = [{"tag": f"agent-{i}", "padding": "x" * 2000} for i in range(600)]
        with self.assertRaises(ValueError) as ctx:
            itr.do_import(huge_payload, "https://hub.dtcdev.click/api/agents/import", "fake-token")
        self.assertIn("Payload too large: exceeds 1MB", str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
