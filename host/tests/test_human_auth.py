import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

HOST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, HOST_DIR)

from agent_hub_host import config, human_auth, auth


def _code_response(url="https://hub.example.com"):
    resp = MagicMock()
    resp.headers = {"Content-Type": "application/json"}
    resp.read.return_value = json.dumps({
        "device_code": "dev-human-1",
        "user_code": "ABCD-EFGH",
        "verification_uri": url + "/device",
        "verification_uri_complete": url + "/device?code=ABCD-EFGH",
        "interval": 0,
        "expires_in": 60,
    }).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


def _token_response(access="human-acc-1", refresh="human-ref-1"):
    resp = MagicMock()
    resp.headers = {"Content-Type": "application/json"}
    resp.read.return_value = json.dumps({
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "bus:send bus:read fleet:launch",
    }).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


class TestVendoredLoop(unittest.TestCase):
    def test_vendored_module_loads_without_checkout_env(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DTC_AUTH_HOME", "PYTHONPATH")}
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from agent_hub_host import _vendored_device_login as f; "
            "assert f.GRANT_TYPE == 'urn:ietf:params:oauth:grant-type:device_code'; "
            "assert callable(f.device_login)"
        )
        res = subprocess.run([sys.executable, "-c", code, HOST_DIR],
                             capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(res.returncode, 0, f"vendored import failed: {res.stderr}")

    def test_vendored_matches_shared_grant_and_errors(self):
        from agent_hub_host import _vendored_device_login as v
        import importlib.util
        shared_path = Path("/home/alexey/git/auth/dtc_device_login/__init__.py")
        if not shared_path.is_file():
            self.skipTest("shared checkout absent")
        spec = importlib.util.spec_from_file_location("shared_dtc_dl", str(shared_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(v.GRANT_TYPE, mod.GRANT_TYPE)
        self.assertEqual(v.SLOW_DOWN_SECONDS, mod.SLOW_DOWN_SECONDS)
        # Same RFC 8628 error handling: pending/slow_down continue, denied/expired raise.
        for code in ("authorization_pending", "slow_down"):
            calls = {"n": 0}

            def poster(url, body):
                calls["n"] += 1
                if "device" in url and "token" not in url:
                    return 200, {"device_code": "d", "user_code": "U",
                                 "verification_uri": "https://x/device",
                                 "interval": 0, "expires_in": 60}
                if calls["n"] < 3:
                    return 400, {"error": code}
                return 200, {"access_token": "a"}

            out = v.device_login("https://x", poster, start_path="/api/device/code",
                                 token_path="/api/device/token",
                                 sleeper=lambda s: None, clock=self._clock())
            self.assertEqual(out, {"access_token": "a"})

    def _clock(self):
        t = [0.0]
        def clock():
            t[0] += 0.5
            return t[0]
        return clock


class TestHumanLogin(unittest.TestCase):
    def test_human_login_writes_human_not_host(self):
        self.assertEqual(config.load_human_config(), {})
        self.assertEqual(config.load_host_config(), {})
        with patch("urllib.request.OpenerDirector.open",
                   side_effect=[_code_response(), _token_response()]):
            out = io.StringIO()
            with patch.object(sys, "stdout", out):
                human_auth.human_login("https://hub.example.com", "dev-box")
        printed = out.getvalue()
        self.assertIn("ABCD-EFGH", printed)
        self.assertIn("https://hub.example.com/device", printed)
        self.assertNotIn("human-acc-1", printed)
        self.assertNotIn("human-ref-1", printed)
        human = config.load_human_config()
        self.assertEqual(human.get("access_token"), "human-acc-1")
        self.assertEqual(human.get("refresh_token"), "human-ref-1")
        self.assertEqual(human.get("base_url"), "https://hub.example.com")
        self.assertEqual(human.get("client_kind"), "human")
        self.assertEqual(human.get("client_name"), "dev-box")
        # Host file untouched.
        self.assertEqual(config.load_host_config(), {})
        # Active profile is now human.
        self.assertEqual(config.load_config().get("access_token"), "human-acc-1")
        self.assertEqual(auth.get_access_token(), "human-acc-1")

    def test_human_login_sends_kind_human_with_label(self):
        bodies = {}

        def opener(req, timeout=None):
            payload = json.loads(req.data.decode("utf-8")) if req.data else {}
            if req.full_url.endswith("/api/device/code"):
                bodies.update(payload)
                return _code_response()
            if req.full_url.endswith("/api/device/token"):
                return _token_response("a2", "r2")
            raise AssertionError(req.full_url)

        with patch("urllib.request.OpenerDirector.open", side_effect=opener):
            with patch.object(sys, "stdout", io.StringIO()):
                human_auth.human_login("https://hub.example.com", "grove-box")
        self.assertEqual(bodies.get("client_kind"), "human")
        self.assertEqual(bodies.get("label"), "grove-box")
        self.assertEqual(bodies.get("name"), "grove-box")
        self.assertIn("bus:send", bodies.get("requested_scopes", []))


class TestHumanStatusLogout(unittest.TestCase):
    def _write_human(self, **over):
        cfg = {"base_url": "https://hub.example.com", "access_token": "h-acc",
               "refresh_token": "h-ref", "client_kind": "human",
               "client_name": "dev", "requested_scopes": ["bus:send", "bus:read"]}
        cfg.update(over)
        config.save_human_config(cfg)

    def test_status_not_logged_in_without_human(self):
        config.save_host_config({"base_url": "https://hub.example.com",
                                 "access_token": "host-tok", "client_kind": "host"})
        st = human_auth.human_status()
        self.assertEqual(st, {"logged_in": False})

    def test_status_returns_safe_identity(self):
        self._write_human()
        me = {"sub": "u123", "email": "o@example.com", "actor": "human:u123",
              "kind": "human", "access_token": "MUST-NOT-LEAK"}
        with patch("agent_hub_host.human_auth._authed_get", return_value=me):
            st = human_auth.human_status()
        self.assertTrue(st["logged_in"])
        self.assertEqual(st["actor"], "human:u123")
        self.assertNotIn("access_token", json.dumps(st))

    def test_status_refreshes_on_401_and_rotates_human_file(self):
        self._write_human()
        seen = {}

        def fake_http(req, timeout=10.0, _token_exchange=None):
            url = req.full_url
            if url.endswith("/api/auth/me"):
                authz = req.get_header("Authorization")
                if authz == "Bearer h-acc":
                    raise urllib.error.HTTPError(url, 401, "x", {}, io.BytesIO(b""))
                seen["retried"] = True
                return {"sub": "u1", "kind": "human"}
            if url.endswith("/api/token/refresh"):
                data = {"access_token": "h-acc-2", "refresh_token": "h-ref-2"}
                if _token_exchange is not None:
                    _token_exchange.record(data)
                return data
            raise AssertionError(url)

        with patch("agent_hub_host.api.safe_http_json", side_effect=fake_http):
            st = human_auth.human_status()
        self.assertTrue(st["logged_in"])
        self.assertTrue(seen.get("retried"))
        human = config.load_human_config()
        self.assertEqual(human["access_token"], "h-acc-2")
        self.assertEqual(human["refresh_token"], "h-ref-2")
        # Host untouched (still empty here).
        self.assertEqual(config.load_host_config(), {})

    def test_logout_revokes_human_and_preserves_host(self):
        self._write_human()
        config.save_host_config({"base_url": "https://hub.example.com",
                                 "access_token": "host-tok", "client_kind": "host"})
        posted = {}

        def fake_http(req, timeout=10.0, _token_exchange=None):
            posted["url"] = req.full_url
            posted["auth"] = req.get_header("Authorization")
            self.assertEqual(req.get_method(), "POST")
            return {"status": "revoked"}

        with patch("agent_hub_host.api.safe_http_json", side_effect=fake_http):
            res = human_auth.human_logout()
        self.assertTrue(res["logged_out"])
        self.assertTrue(posted["url"].endswith("/api/auth/logout"))
        self.assertEqual(posted["auth"], "Bearer h-acc")
        self.assertEqual(config.load_human_config(), {})
        host = config.load_host_config()
        self.assertEqual(host.get("access_token"), "host-tok")

    def test_logout_already_logged_out_preserves_host(self):
        config.save_host_config({"base_url": "https://hub.example.com",
                                 "access_token": "host-tok", "client_kind": "host"})
        with patch("agent_hub_host.api.safe_http_json") as mock_http:
            res = human_auth.human_logout()
            mock_http.assert_not_called()
        self.assertTrue(res.get("already"))
        self.assertEqual(config.load_host_config().get("access_token"), "host-tok")


class TestHostUntouched(unittest.TestCase):
    def test_host_device_login_writes_host_only(self):
        with patch("urllib.request.OpenerDirector.open",
                   side_effect=[_code_response(), _token_response("host-a", "host-r")]):
            with patch.object(sys, "stdout", io.StringIO()):
                auth.device_login("https://hub.example.com", "host", "h1", "myhost", ["host"])
        host = config.load_host_config()
        self.assertEqual(host.get("access_token"), "host-a")
        self.assertEqual(config.load_human_config(), {})

    def test_explicit_config_file_wins(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"access_token": "tok", "client_kind": "host"}, f)
            path = f.name
        try:
            with patch.dict(os.environ, {"HUB_CONFIG_FILE": path}):
                self.assertEqual(str(config.get_config_path()), path)
                self.assertEqual(str(config.get_human_config_path()), path)
        finally:
            os.unlink(path)


class TestFreshCli(unittest.TestCase):
    def test_auth_status_json_not_logged_in_fresh_home(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = HOST_DIR
        env.pop("DTC_AUTH_HOME", None)
        env.pop("HUB_CONFIG_FILE", None)
        res = subprocess.run(
            [sys.executable, "-m", "agent_hub_host", "auth", "status", "--json"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(json.loads(res.stdout), {"logged_in": False})

    def test_cli_help_lists_auth_and_version(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = HOST_DIR
        env.pop("DTC_AUTH_HOME", None)
        for argv in (["--help"], ["--version"], ["auth", "--help"],
                     ["auth", "login", "--help"], ["auth", "status", "--help"],
                     ["auth", "logout", "--help"]):
            res = subprocess.run(
                [sys.executable, "-m", "agent_hub_host"] + argv,
                capture_output=True, text=True, env=env, timeout=15,
            )
            self.assertEqual(res.returncode, 0, f"{argv}: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
