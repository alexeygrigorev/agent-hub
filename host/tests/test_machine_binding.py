"""Authenticated transitions use mock HTTP and real, private synthetic config files."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import api, auth, config


ABSENT = object()


def response(data):
    res = MagicMock()
    res.headers = {"Content-Type": "application/json"}
    res.read.return_value = json.dumps(data).encode()
    res.__enter__.return_value = res
    return res


class TestMachineBinding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config" / "host.json"
        # Intake 6b624e65 AC3: redirect the human profile as well. Patching only
        # get_config_path left auth._active_profile() free to resolve the real
        # ~/.config/agent-hub/cli.json through human-first load_config() and
        # save refresh tokens there (the 2026-10-27 cli.json clobber).
        self.human_path = Path(self.tmp.name) / "config" / "cli.json"
        self.addCleanup(patch.stopall)
        patch.object(config, "get_config_path", return_value=self.path).start()
        # save_config/load_config prefer the worker profile path, which they
        # resolve from APLEXER_TAG/APLEXER_WORKSPACE and which bypasses the
        # patched get_config_path: without this pin, a suite run from an
        # aplexer session shell writes fixtures into that session's real
        # hub-worker-auth file (and destroys its credential).
        patch("agent_hub_host.worker_auth.config_path", return_value=None).start()
        patch.object(config, "get_human_config_path", return_value=self.human_path).start()
        # Reuse the real device polling loop without sleeping or printing codes.
        flow = auth._device_flow()
        real_login = flow.device_login
        patch.object(flow, "device_login", side_effect=lambda *a, **kw: real_login(
            *a, **{**kw, "sleeper": lambda _: None, "prompt": lambda _: None})).start()
        self.old = {
            "base_url": "https://hub.example", "access_token": "fixture-old-access",
            "refresh_token": "fixture-old-refresh", "machine_id": "old-machine",
            "options": {"keep": True}, "client_kind": "agent", "client_name": "project:worker",
            "requested_scopes": auth.AGENT_SCOPES,
        }
        config.save_config(self.old)

    def payload(self, machine=ABSENT):
        data = {"access_token": "fixture-new-access", "refresh_token": "fixture-new-refresh",
                "token_type": "Bearer", "expires_in": 30, "scope": "bus:read"}
        if machine is not ABSENT:
            data["machine_id"] = machine
        return data

    def login(self, payload, base_url="https://hub.example", after_open=None):
        start = {"device_code": "fixture-device-code", "user_code": "FIXTURE",
                 "expires_in": 60, "interval": 1}
        replies = iter([response(start), response(payload)])

        def opened(req, **kwargs):
            res = next(replies)
            if after_open and req.full_url.endswith("/api/device/token"):
                after_open()
            return res

        with patch("urllib.request.OpenerDirector.open", side_effect=opened), contextlib.redirect_stdout(io.StringIO()):
            auth.device_login(base_url, "agent", "project:worker", "fixture-host", auth.AGENT_SCOPES)

    def refresh(self, payload, error=None):
        with patch("urllib.request.OpenerDirector.open", side_effect=error, return_value=response(payload)):
            return auth.refresh_token()

    def assert_unchanged(self, before):
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(config.load_config(), self.old)

    def test_device_login_changed_null_and_absent_binding(self):
        for machine, expected in [("new-machine", "new-machine"), (None, ABSENT), (ABSENT, "old-machine")]:
            with self.subTest(machine=machine):
                config.save_config(self.old)
                self.login(self.payload(machine))
                loaded = config.load_config()
                self.assertEqual(loaded["access_token"], "fixture-new-access")
                self.assertEqual(loaded["refresh_token"], "fixture-new-refresh")
                self.assertEqual(loaded["options"], self.old["options"])
                self.assertEqual(loaded["base_url"], self.old["base_url"])
                if expected is ABSENT:
                    self.assertNotIn("machine_id", loaded)
                else:
                    self.assertEqual(loaded["machine_id"], expected)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_refresh_changed_null_and_absent_binding_with_rotated_token(self):
        for machine, expected in [("new-machine", "new-machine"), (None, ABSENT), (ABSENT, "old-machine")]:
            with self.subTest(machine=machine):
                config.save_config(self.old)
                self.assertEqual(self.refresh(self.payload(machine)), "fixture-new-access")
                loaded = config.load_config()
                self.assertEqual(loaded["access_token"], "fixture-new-access")
                self.assertEqual(loaded["options"], self.old["options"])
                if expected is ABSENT:
                    self.assertNotIn("machine_id", loaded)
                else:
                    self.assertEqual(loaded["machine_id"], expected)

    def test_refresh_missing_or_null_refresh_token_preserves_or_removes(self):
        for value in (ABSENT, None):
            with self.subTest(value=value):
                config.save_config(self.old)
                payload = self.payload(None)
                if value is ABSENT:
                    payload.pop("refresh_token")
                else:
                    payload["refresh_token"] = None
                self.refresh(payload)
                loaded = config.load_config()
                self.assertEqual(loaded.get("refresh_token"), self.old["refresh_token"] if value is ABSENT else None)
                self.assertNotIn("machine_id", loaded)

    def test_device_missing_refresh_token_removes_old_refresh(self):
        payload = self.payload(None)
        payload.pop("refresh_token")
        self.login(payload)
        self.assertNotIn("refresh_token", config.load_config())

    def test_invalid_response_leaves_private_config_unchanged(self):
        bad = [None, [], {}, {"access_token": None}, {"access_token": ""},
               {"access_token": "rejected token"}, {"access_token": "rejected\ntoken"},
               {"access_token": "ok", "refresh_token": 2},
               {"access_token": "ok", "error": "rejected-token"}]
        bad += [dict(self.payload(), machine_id=val) for val in ("", " ", "bad id", False, 3, {}, [])]
        bad += [dict(self.payload(None), **{key: value}) for key, value in
                [("token_type", "rejected-token"), ("expires_in", True), ("expires_in", -1),
                 ("scope", ["host"]), ("scope", "rejected\nmetadata")]]
        for operation in (self.login, self.refresh):
            for payload in bad:
                with self.subTest(operation=operation.__name__, payload=payload):
                    before = self.path.read_bytes()
                    with self.assertRaises((ValueError, RuntimeError)) as raised:
                        operation(payload)
                    self.assertNotIn("rejected", str(raised.exception))
                    self.assert_unchanged(before)
                    config.update_config(options={"keep": True})
                    self.assert_unchanged(before)

    def test_foreign_login_origin_rejected_before_http(self):
        before = self.path.read_bytes()
        with patch("urllib.request.OpenerDirector.open") as opened:
            with self.assertRaisesRegex(ValueError, "origin mismatch"):
                auth.device_login("https://foreign.example", "agent", "project:worker", "fixture", [])
            opened.assert_not_called()
        self.assert_unchanged(before)

    def test_equivalent_same_origin_and_trailing_slash(self):
        self.login(self.payload("new-machine"), "https://HUB.example:443/")
        self.assertEqual(config.load_config()["machine_id"], "new-machine")
        self.refresh(self.payload(None))
        self.assertNotIn("machine_id", config.load_config())

    def test_arbitrary_http_response_or_request_json_does_not_authorize_update(self):
        for url in ("https://hub.example/api/device/token", "https://foreign.example/api/device/token",
                    "https://hub.example/api/token/refresh", "https://hub.example/arbitrary"):
            with self.subTest(url=url):
                req = urllib.request.Request(url, data=json.dumps(self.payload("request-machine")).encode())
                with patch("urllib.request.OpenerDirector.open", return_value=response(self.payload("new-machine"))):
                    api.safe_http_json(req)
                config.update_config(access_token="fixture-new-access")
                self.assertEqual(config.load_config()["machine_id"], "old-machine")
                with self.assertRaisesRegex(ValueError, "Conflicting machine_id"):
                    config.update_config(machine_id="new-machine")
                with self.assertRaisesRegex(ValueError, "Invalid machine_id"):
                    config.update_config(machine_id=None)

    def exchange(self, path="/api/device/token"):
        return config._TokenExchange(self.old["base_url"], path, config.load_config())

    def receive(self, exchange, payload):
        req = urllib.request.Request(exchange.url, data=b"{}")
        with patch("urllib.request.OpenerDirector.open", return_value=response(payload)):
            return api.safe_http_json(req, _token_exchange=exchange)

    def test_receipt_is_one_shot_even_after_save_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                config.save_config(self.old)
                before = self.path.read_bytes()
                exchange = self.exchange()
                result = self.receive(exchange, self.payload("new-machine"))
                if fail:
                    with patch.object(config.private_fs, "atomic_write", side_effect=PermissionError("fixture failure")):
                        with self.assertRaisesRegex(RuntimeError, "Config save failed"):
                            exchange.commit(result)
                    self.assert_unchanged(before)
                else:
                    exchange.commit(result)
                with self.assertRaisesRegex(ValueError, "response mismatch"):
                    exchange.commit(result)
                config.update_config(options={"keep": True})
                self.assertEqual(config.load_config().get("machine_id"), "old-machine" if fail else "new-machine")

    def test_different_response_or_changed_token_cannot_consume_receipt(self):
        for replace in (True, False):
            with self.subTest(replace=replace):
                before = self.path.read_bytes()
                exchange = self.exchange()
                result = self.receive(exchange, self.payload(None))
                if replace:
                    result = dict(result)
                result["access_token"] = "fixture-different-access"
                with self.assertRaisesRegex(ValueError, "response mismatch"):
                    exchange.commit(result)
                self.assert_unchanged(before)
                with self.assertRaises(ValueError):
                    exchange.commit(result)

    def test_wrong_endpoint_method_origin_query_cannot_record_receipt(self):
        for url, method in [("https://foreign.example/api/device/token", "POST"),
                            ("https://hub.example/other/api/device/token", "POST"),
                            ("https://hub.example/api/device/token?x=1", "POST"),
                            ("https://hub.example/api/token/refresh", "POST"),
                            ("https://hub.example/api/device/token", "GET")]:
            with self.subTest(url=url, method=method):
                before = self.path.read_bytes()
                exchange = self.exchange()
                req = urllib.request.Request(url, data=b"{}", method=method)
                with patch("urllib.request.OpenerDirector.open") as opened:
                    with self.assertRaisesRegex(ValueError, "request mismatch"):
                        api.safe_http_json(req, _token_exchange=exchange)
                    opened.assert_not_called()
                with self.assertRaises(ValueError):
                    exchange.commit(self.payload(None))
                self.assert_unchanged(before)

    def test_failed_later_request_resets_previous_receipt(self):
        exchange = self.exchange()
        result = self.receive(exchange, self.payload(None))
        with patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError("fixture")):
            with self.assertRaises(urllib.error.URLError):
                api.safe_http_json(urllib.request.Request(exchange.url, data=b"{}"), _token_exchange=exchange)
        with self.assertRaises(ValueError):
            exchange.commit(result)
        self.assertEqual(config.load_config(), self.old)

    def test_delayed_earlier_response_cannot_overwrite_newer_config(self):
        earlier = self.exchange()
        earlier_result = self.receive(earlier, self.payload("earlier-machine"))
        self.login(self.payload("newer-machine"))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Config changed"):
            earlier.commit(earlier_result)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(config.load_config()["machine_id"], "newer-machine")

    def test_config_changed_while_login_in_flight_is_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, "Config changed"):
            self.login(self.payload(None), after_open=lambda: config.update_config(options={"newer": True}))
        loaded = config.load_config()
        self.assertEqual(loaded["options"], {"newer": True})
        self.assertEqual(loaded["machine_id"], "old-machine")
        self.assertEqual(loaded["access_token"], self.old["access_token"])

    def test_mocked_or_cached_success_without_transport_receipt_is_refused(self):
        before = self.path.read_bytes()
        with patch.object(auth._device_flow(), "device_login", return_value=self.payload(None)):
            with self.assertRaisesRegex(ValueError, "response mismatch"):
                auth.device_login(self.old["base_url"], "agent", "project:worker", "fixture", [])
        with patch.object(api, "safe_http_json", return_value=self.payload("new-machine")):
            with self.assertRaisesRegex(ValueError, "response mismatch"):
                auth.refresh_token()
        self.assert_unchanged(before)

    def test_refresh_http_network_json_and_content_type_failures_preserve_binding(self):
        errors = [urllib.error.HTTPError("fixture", code, "fixture", {}, io.BytesIO(b'{}'))
                  for code in (403, 429, 500)]
        errors.append(urllib.error.URLError("fixture"))
        for error in errors:
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)):
                before = self.path.read_bytes()
                with self.assertRaises(RuntimeError):
                    self.refresh(None, error)
                self.assert_unchanged(before)
        for body, content_type in [(b"not-json", "application/json"), (b"{}", "text/html")]:
            before = self.path.read_bytes()
            res = response(None)
            res.headers = {"Content-Type": content_type}
            res.read.return_value = body
            with patch("urllib.request.OpenerDirector.open", return_value=res):
                with self.assertRaises((RuntimeError, ValueError)):
                    auth.refresh_token()
            self.assert_unchanged(before)

    def test_revoked_refresh_followed_by_failed_pairing_preserves_config(self):
        for code in (400, 401):
            with self.subTest(code=code):
                before = self.path.read_bytes()
                error = urllib.error.HTTPError("fixture", code, "fixture", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
                with patch.object(auth, "device_login", side_effect=RuntimeError("fixture pairing failure")), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "pairing failure"):
                        self.refresh(None, error)
                self.assert_unchanged(before)

    def test_login_save_failure_is_atomic_and_no_later_update_consumes_binding(self):
        before = self.path.read_bytes()
        with patch.object(config.private_fs, "atomic_write", side_effect=PermissionError("fixture")):
            with self.assertRaisesRegex(RuntimeError, "Config save failed"):
                self.login(self.payload(None))
        self.assert_unchanged(before)
        config.update_config(options={"keep": True})
        self.assert_unchanged(before)


if __name__ == "__main__":
    unittest.main()
